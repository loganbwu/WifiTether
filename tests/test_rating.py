"""Tests for star-rating extraction: XMP sidecar/embedded and Lightroom catalog."""

import sqlite3

import piexif
import pytest
from PIL import Image
from server import (
    FolderHandler,
    _flash_override_from_xmp_bytes,
    _lightroom_poll_once,
    _rating_from_xmp_bytes,
    compute_series,
    get_file_rating,
    get_flash_override,
    get_rating,
    handle_modified,
    process_file,
    query_lightroom_ratings,
    refresh_metadata,
    sse_clients,
    sse_lock,
    state,
    state_lock,
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


def test_catalog_lookup_matches_subfolder_prefixed_filename(lrcat):
    # Lightroom's own schema only ever stores a bare basename -- a filename
    # carrying a subfolder prefix (as ours do once inside a watched folder)
    # must still match by basename, keyed by the original prefixed name.
    result = query_lightroom_ratings(lrcat, ['session1/IMG_0001.CR3'])
    assert result == {'session1/IMG_0001.CR3': 4}


# ---------------------------------------------------------------------------
# _lightroom_poll_once — the poll loop's own per-iteration logic
# ---------------------------------------------------------------------------

def _tracked_photo(filename, rating):
    return {
        'filename': filename, 'path': '', 'flash': True, 'exif_flash': True,
        'timestamp': '2026:01:01 10:00:00', 'has_preview': False, 'aspect': None,
        'error': None, 'rating': rating, 'mtime': 0,
    }


def test_lightroom_poll_once_updates_rating_and_notifies(clean_state, lrcat, sse_queue):
    with state_lock:
        state['lightroom_catalog'] = lrcat
        state['photos'] = [_tracked_photo('IMG_0001.CR3', rating=1)]
        state['series'] = compute_series(state['photos'])

    _lightroom_poll_once()

    assert state['photos'][0]['rating'] == 4
    assert state['series'][0]['base']['rating'] == 4
    assert not sse_queue.empty()


def test_lightroom_poll_once_noop_when_no_catalog_configured(clean_state, sse_queue):
    with state_lock:
        state['lightroom_catalog'] = None
        state['photos'] = [_tracked_photo('IMG_0001.CR3', rating=1)]

    _lightroom_poll_once()
    assert sse_queue.empty()


def test_lightroom_poll_once_noop_when_rating_already_matches(clean_state, lrcat, sse_queue):
    with state_lock:
        state['lightroom_catalog'] = lrcat
        state['photos'] = [_tracked_photo('IMG_0001.CR3', rating=4)]   # already matches catalog
        state['series'] = compute_series(state['photos'])

    _lightroom_poll_once()
    assert sse_queue.empty()


def test_lightroom_poll_once_updates_rating_for_subfolder_photo(clean_state, lrcat, sse_queue):
    with state_lock:
        state['lightroom_catalog'] = lrcat
        state['photos'] = [_tracked_photo('session1/IMG_0001.CR3', rating=1)]
        state['series'] = compute_series(state['photos'])

    _lightroom_poll_once()

    assert state['photos'][0]['rating'] == 4
    assert not sse_queue.empty()


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
# _flash_override_from_xmp_bytes / get_flash_override
# ---------------------------------------------------------------------------

def test_flash_override_fired_keyword():
    data = b'<dc:subject><rdf:Bag><rdf:li>flash_fired</rdf:li></rdf:Bag></dc:subject>'
    assert _flash_override_from_xmp_bytes(data) is True


def test_flash_override_not_fired_keyword():
    data = b'<dc:subject><rdf:Bag><rdf:li>flash_not_fired</rdf:li></rdf:Bag></dc:subject>'
    assert _flash_override_from_xmp_bytes(data) is False


def test_flash_override_absent_returns_none():
    data = b'<dc:subject><rdf:Bag><rdf:li>some_other_keyword</rdf:li></rdf:Bag></dc:subject>'
    assert _flash_override_from_xmp_bytes(data) is None


def test_flash_override_both_keywords_is_ambiguous():
    data = b'<rdf:li>flash_fired</rdf:li><rdf:li>flash_not_fired</rdf:li>'
    assert _flash_override_from_xmp_bytes(data) is None


def test_get_flash_override_sidecar_takes_precedence(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'<rdf:li>flash_not_fired</rdf:li>')
    sidecar = tmp_path / 'photo.xmp'
    sidecar.write_bytes(b'<rdf:li>flash_fired</rdf:li>')

    assert get_flash_override(photo) is True


def test_get_flash_override_none_when_no_keyword(tmp_path):
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'no keywords here')
    assert get_flash_override(photo) is None


def test_process_file_applies_flash_override_at_capture_time(clean_state, make_jpeg):
    # EXIF says no flash (e.g. an off-camera strobe the camera can't detect),
    # but a sidecar written before the file was even scanned overrides it.
    path = make_jpeg('photo.jpg', flash=0, timestamp='2026:01:01 10:00:00')
    path.with_suffix('.xmp').write_bytes(b'<rdf:li>flash_fired</rdf:li>')
    process_file(str(path))

    photo = state['photos'][0]
    assert photo['exif_flash'] is False
    assert photo['flash'] is True


# ---------------------------------------------------------------------------
# refresh_metadata — re-derive an already-tracked photo's rating and flash
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


def test_refresh_metadata_updates_rating_and_notifies(clean_state, make_jpeg, monkeypatch, sse_queue):
    monkeypatch.setattr('server.get_file_rating', lambda path: 2)
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    monkeypatch.setattr('server.get_file_rating', lambda path: 4)
    refresh_metadata('photo.jpg')

    photo = next(p for p in state['photos'] if p['filename'] == 'photo.jpg')
    assert photo['rating'] == 4
    assert state['series'][0]['base']['rating'] == 4
    assert not sse_queue.empty()


def test_refresh_metadata_applies_flash_override(clean_state, make_jpeg, sse_queue):
    # Camera EXIF says flash did not fire (an off-camera strobe it can't detect).
    path = make_jpeg('photo.jpg', flash=0, timestamp='2026:01:01 10:00:00')
    process_file(str(path))
    assert state['photos'][0]['flash'] is False

    path.with_suffix('.xmp').write_bytes(b'<rdf:li>flash_fired</rdf:li>')
    refresh_metadata('photo.jpg')

    photo = next(p for p in state['photos'] if p['filename'] == 'photo.jpg')
    assert photo['flash'] is True
    assert state['series'][0]['base']['flash'] is True


def test_refresh_metadata_noop_when_unchanged(clean_state, make_jpeg, monkeypatch, sse_queue):
    monkeypatch.setattr('server.get_file_rating', lambda path: 3)
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))
    while not sse_queue.empty():   # drain the 'update' notification from process_file
        sse_queue.get_nowait()

    refresh_metadata('photo.jpg')   # rating still 3, no override — nothing changed
    assert sse_queue.empty()


def test_refresh_metadata_unknown_filename_is_noop(clean_state, sse_queue):
    refresh_metadata('does_not_exist.jpg')
    assert sse_queue.empty()


# ---------------------------------------------------------------------------
# handle_modified — routes sidecar/file changes to refresh_metadata
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


def test_handle_modified_picks_up_flash_override_too(clean_state, make_jpeg):
    path = make_jpeg('photo.jpg', flash=0, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    sidecar = path.with_suffix('.xmp')
    sidecar.write_bytes(b'<rdf:li>flash_fired</rdf:li>')
    handle_modified(str(sidecar))

    photo = next(p for p in state['photos'] if p['filename'] == 'photo.jpg')
    assert photo['flash'] is True


def test_handle_modified_ignores_untracked_file(clean_state, tmp_path):
    untracked = tmp_path / 'untracked.jpg'
    untracked.write_bytes(b'not tracked')
    handle_modified(str(untracked))   # should not raise
    assert state['photos'] == []


def test_handle_modified_skips_refresh_when_file_never_stabilises(clean_state, make_jpeg, monkeypatch):
    path = make_jpeg('photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    calls = []
    monkeypatch.setattr('server.wait_for_file_stable', lambda *a, **kw: False)
    monkeypatch.setattr('server.refresh_metadata', lambda *a, **kw: calls.append(a))
    handle_modified(str(path))

    assert calls == []


# ---------------------------------------------------------------------------
# refresh_metadata(full_rescan=True) / handle_modified on the photo file
# itself -- e.g. a Lightroom re-export overwriting the same path after
# further edits, where the pixel content and embedded EXIF can both change.
# ---------------------------------------------------------------------------

def _write_jpeg(path, size, flash, timestamp='2026:01:01 10:00:00'):
    exif_bytes = piexif.dump({
        '0th': {},
        'Exif': {
            piexif.ExifIFD.Flash: flash,
            piexif.ExifIFD.DateTimeOriginal: timestamp.encode('ascii'),
        },
        '1st': {}, 'GPS': {}, 'Interop': {},
    })
    Image.new('RGB', size, (200, 100, 50)).save(path, exif=exif_bytes)


def test_refresh_metadata_full_rescan_updates_everything(clean_state, tmp_path):
    path = tmp_path / 'photo.jpg'
    _write_jpeg(path, (100, 100), flash=0, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    original = state['photos'][0]
    assert original['flash'] is False
    assert original['aspect'] == 1.0
    assert original['timestamp'] == '2026:01:01 10:00:00'

    # Re-exported after further edits: different crop (aspect), flash now
    # detected, and a different capture timestamp.
    _write_jpeg(path, (200, 100), flash=1, timestamp='2026:01:01 11:00:00')
    refresh_metadata('photo.jpg', full_rescan=True)

    photo = state['photos'][0]
    assert photo['flash'] is True
    assert photo['aspect'] == 2.0
    assert photo['timestamp'] == '2026:01:01 11:00:00'


def test_refresh_metadata_full_rescan_clears_stale_error(clean_state, tmp_path):
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'not a real jpeg')   # unreadable at first scan
    process_file(str(path))
    assert state['photos'][0]['error'] == 'Could not read EXIF'

    _write_jpeg(path, (100, 100), flash=1)
    refresh_metadata('photo.jpg', full_rescan=True)

    photo = state['photos'][0]
    assert photo['error'] is None
    assert photo['flash'] is True


def test_handle_modified_triggers_full_rescan_for_tracked_image_file(clean_state, tmp_path):
    path = tmp_path / 'photo.jpg'
    _write_jpeg(path, (100, 100), flash=0)
    process_file(str(path))
    assert state['photos'][0]['aspect'] == 1.0

    _write_jpeg(path, (150, 100), flash=1)
    handle_modified(str(path))   # the image file itself changed, not a sidecar

    photo = state['photos'][0]
    assert photo['aspect'] == 1.5
    assert photo['flash'] is True


def test_refresh_metadata_full_rescan_notifies_on_pixel_only_change(clean_state, tmp_path, sse_queue):
    # A pure exposure/tone tweak re-export: same dimensions, same flash, same
    # timestamp -- every "visible" field the old no-op check compared is
    # identical, but the pixel content (and therefore the thumbnail clients
    # should display) has genuinely changed.
    path = tmp_path / 'photo.jpg'
    _write_jpeg(path, (100, 100), flash=1)
    process_file(str(path))
    while not sse_queue.empty():
        sse_queue.get_nowait()
    mtime_before = state['photos'][0]['mtime']

    import time as time_module
    time_module.sleep(0.01)   # ensure a distinct mtime
    _write_jpeg(path, (100, 100), flash=1)   # same size/flash/timestamp, re-written
    refresh_metadata('photo.jpg', full_rescan=True)

    photo = state['photos'][0]
    assert photo['mtime'] != mtime_before
    assert not sse_queue.empty(), 'clients must be notified even when only pixel content changed'


# ---------------------------------------------------------------------------
# FolderHandler — watchdog event routing (on_created/on_modified each spawn a
# background thread, so these poll briefly rather than asserting instantly)
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(self, src_path, is_directory=False):
        self.src_path = src_path
        self.is_directory = is_directory


def _wait_until(predicate, timeout=2.0, interval=0.02):
    import time as time_module
    deadline = time_module.time() + timeout
    while time_module.time() < deadline:
        if predicate():
            return True
        time_module.sleep(interval)
    return False


def test_folder_handler_on_created_routes_xmp_to_handle_modified(monkeypatch):
    calls = []
    monkeypatch.setattr('server.handle_modified', lambda path: calls.append(('handle_modified', path)))
    monkeypatch.setattr('server.process_file', lambda *a, **kw: calls.append(('process_file', a)))

    FolderHandler().on_created(_FakeEvent('/tmp/photo.xmp'))

    assert _wait_until(lambda: len(calls) == 1)
    assert calls == [('handle_modified', '/tmp/photo.xmp')]


def test_folder_handler_on_created_routes_image_to_process_file(monkeypatch):
    calls = []
    monkeypatch.setattr('server.handle_modified', lambda path: calls.append(('handle_modified', path)))
    monkeypatch.setattr('server.process_file', lambda *a, **kw: calls.append(('process_file', a)))

    FolderHandler().on_created(_FakeEvent('/tmp/photo.jpg'))

    assert _wait_until(lambda: len(calls) == 1)
    assert calls[0] == ('process_file', ('/tmp/photo.jpg',))


def test_folder_handler_on_created_ignores_directories(monkeypatch):
    calls = []
    monkeypatch.setattr('server.handle_modified', lambda path: calls.append(path))
    monkeypatch.setattr('server.process_file', lambda *a, **kw: calls.append(a))

    FolderHandler().on_created(_FakeEvent('/tmp/somedir', is_directory=True))

    assert not _wait_until(lambda: len(calls) > 0, timeout=0.3)


def test_folder_handler_on_modified_routes_to_handle_modified(monkeypatch):
    calls = []
    monkeypatch.setattr('server.handle_modified', lambda path: calls.append(path))

    FolderHandler().on_modified(_FakeEvent('/tmp/photo.jpg'))

    assert _wait_until(lambda: len(calls) == 1)
    assert calls == ['/tmp/photo.jpg']


def test_folder_handler_on_modified_ignores_directories(monkeypatch):
    calls = []
    monkeypatch.setattr('server.handle_modified', lambda path: calls.append(path))

    FolderHandler().on_modified(_FakeEvent('/tmp/somedir', is_directory=True))

    assert not _wait_until(lambda: len(calls) > 0, timeout=0.3)
