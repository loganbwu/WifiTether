"""Tests for star-rating extraction: XMP sidecar/embedded and Lightroom catalog."""

import sqlite3

import pytest
from server import (
    _rating_from_xmp_bytes,
    get_file_rating,
    get_rating,
    handle_modified,
    process_file,
    query_lightroom_ratings,
    refresh_rating,
    sse_clients,
    sse_lock,
    state,
)


# ---------------------------------------------------------------------------
# _rating_from_xmp_bytes
# ---------------------------------------------------------------------------

def test_rating_from_attribute_form():
    data = b'<rdf:Description xmp:Rating="4" other="x"/>'
    assert _rating_from_xmp_bytes(data) == 4


def test_rating_from_element_form():
    data = b'<xmp:Rating>3</xmp:Rating>'
    assert _rating_from_xmp_bytes(data) == 3


def test_rating_negative_rejected():
    data = b'xmp:Rating="-1"'
    assert _rating_from_xmp_bytes(data) == -1


def test_rating_missing_returns_none():
    assert _rating_from_xmp_bytes(b'no xmp metadata here') is None


# ---------------------------------------------------------------------------
# get_file_rating — sidecar vs. embedded
# ---------------------------------------------------------------------------

def test_sidecar_rating_takes_precedence(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'fake-jpeg-bytes xmp:Rating="1"')
    sidecar = tmp_path / 'photo.xmp'
    sidecar.write_bytes(b'<xmp:Rating>5</xmp:Rating>')

    assert get_file_rating(photo) == 5


def test_falls_back_to_embedded_when_no_sidecar(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'fake-jpeg-bytes xmp:Rating="2"')

    assert get_file_rating(photo) == 2


def test_falls_back_to_embedded_when_sidecar_has_no_rating(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'fake-jpeg-bytes xmp:Rating="2"')
    sidecar = tmp_path / 'photo.xmp'
    sidecar.write_bytes(b'<rdf:Description/>')   # no rating in sidecar

    assert get_file_rating(photo) == 2


def test_no_rating_anywhere_returns_none(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'fake-jpeg-bytes, no metadata')

    assert get_file_rating(photo) is None


# ---------------------------------------------------------------------------
# query_lightroom_ratings
# ---------------------------------------------------------------------------

@pytest.fixture
def lrcat(tmp_path):
    """A minimal synthetic Lightroom catalog with the tables/columns this
    app relies on."""
    path = tmp_path / 'Catalog.lrcat'
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, baseName TEXT, extension TEXT)')
    conn.execute('CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, rootFile INTEGER, rating REAL)')
    conn.execute("INSERT INTO AgLibraryFile VALUES (1, 'IMG_0001', 'CR3')")
    conn.execute("INSERT INTO AgLibraryFile VALUES (2, 'IMG_0002', 'CR3')")
    conn.execute('INSERT INTO Adobe_images VALUES (1, 1, 4.0)')
    conn.execute('INSERT INTO Adobe_images VALUES (2, 2, NULL)')   # unrated
    conn.commit()
    conn.close()
    return str(path)


def test_catalog_lookup_returns_rating(lrcat):
    result = query_lightroom_ratings(lrcat, ['IMG_0001.CR3'])
    assert result == {'IMG_0001.CR3': 4}


def test_catalog_lookup_is_case_insensitive(lrcat):
    result = query_lightroom_ratings(lrcat, ['img_0001.cr3'])
    assert result == {'img_0001.cr3': 4}


def test_catalog_unrated_row_omitted(lrcat):
    result = query_lightroom_ratings(lrcat, ['IMG_0002.CR3'])
    assert result == {}


def test_catalog_missing_file_returns_empty_dict(tmp_path):
    result = query_lightroom_ratings(str(tmp_path / 'nonexistent.lrcat'), ['IMG_0001.CR3'])
    assert result == {}


def test_catalog_unexpected_schema_returns_empty_dict(tmp_path):
    path = tmp_path / 'Bad.lrcat'
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE unrelated (x INTEGER)')
    conn.commit()
    conn.close()

    result = query_lightroom_ratings(str(path), ['IMG_0001.CR3'])
    assert result == {}


def test_empty_filenames_short_circuits(lrcat):
    assert query_lightroom_ratings(lrcat, []) == {}


# ---------------------------------------------------------------------------
# get_rating — catalog vs. file fallback
# ---------------------------------------------------------------------------

def test_get_rating_prefers_catalog(tmp_path, lrcat):
    photo = tmp_path / 'IMG_0001.CR3'
    photo.write_bytes(b'fake-cr3-bytes xmp:Rating="1"')   # would say 1 if read from file
    assert get_rating(photo, lrcat) == 4                  # catalog says 4 — wins


def test_get_rating_falls_back_to_file_when_not_in_catalog(tmp_path, lrcat):
    photo = tmp_path / 'not_in_catalog.jpg'
    photo.write_bytes(b'fake-jpeg-bytes xmp:Rating="3"')
    assert get_rating(photo, lrcat) == 3


def test_get_rating_no_catalog_uses_file(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'fake-jpeg-bytes xmp:Rating="2"')
    assert get_rating(photo, None) == 2


# ---------------------------------------------------------------------------
# refresh_rating — re-derive an already-tracked photo's rating
# ---------------------------------------------------------------------------

@pytest.fixture
def sse_queue():
    import queue
    q = queue.Queue()
    with sse_lock:
        sse_clients.append(q)
    yield q
    with sse_lock:
        if q in sse_clients:
            sse_clients.remove(q)


def test_refresh_rating_updates_state_and_notifies(clean_state, make_jpeg, monkeypatch, sse_queue):
    monkeypatch.setattr('server.get_file_rating', lambda path: 2)
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    monkeypatch.setattr('server.get_file_rating', lambda path: 4)
    refresh_rating('photo.jpg')

    photo = next(p for p in state['photos'] if p['filename'] == 'photo.jpg')
    assert photo['rating'] == 4
    assert state['series'][0]['base']['rating'] == 4
    assert not sse_queue.empty()


def test_refresh_rating_noop_when_unchanged(clean_state, make_jpeg, monkeypatch, sse_queue):
    monkeypatch.setattr('server.get_file_rating', lambda path: 3)
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))
    while not sse_queue.empty():   # drain the 'update' notification from process_file
        sse_queue.get_nowait()

    refresh_rating('photo.jpg')   # rating still 3 — nothing changed
    assert sse_queue.empty()


def test_refresh_rating_unknown_filename_is_noop(clean_state, sse_queue):
    refresh_rating('does_not_exist.jpg')
    assert sse_queue.empty()


# ---------------------------------------------------------------------------
# handle_modified — routes sidecar/file changes to refresh_rating
# ---------------------------------------------------------------------------

def test_handle_modified_matches_sidecar_to_tracked_photo(clean_state, make_jpeg):
    # No sidecar/embedded rating yet at capture time — process_file's real
    # get_file_rating naturally returns None for a metadata-free JPEG.
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    sidecar = path.with_suffix('.xmp')
    sidecar.write_bytes(b'<xmp:Rating>5</xmp:Rating>')
    handle_modified(str(sidecar))

    photo = next(p for p in state['photos'] if p['filename'] == 'photo.jpg')
    assert photo['rating'] == 5


def test_handle_modified_ignores_untracked_file(clean_state, tmp_path):
    untracked = tmp_path / 'untracked.jpg'
    untracked.write_bytes(b'not tracked')
    handle_modified(str(untracked))   # should not raise
    assert state['photos'] == []
