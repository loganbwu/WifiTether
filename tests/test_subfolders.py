"""Tests for recursive subfolder support: scanning, live-watch routing, and
filename identity when different subfolders can share a basename."""

from pathlib import Path

from server import (
    _relative_name,
    handle_modified,
    process_file,
    scan_folder,
    state,
    state_lock,
)


def _set_folder(folder):
    with state_lock:
        state['folder'] = str(folder)


# ---------------------------------------------------------------------------
# _relative_name
# ---------------------------------------------------------------------------

def test_relative_name_uses_posix_path_relative_to_watched_folder(clean_state, tmp_path):
    _set_folder(tmp_path)
    nested = tmp_path / 'session1' / 'photo.jpg'
    assert _relative_name(nested) == 'session1/photo.jpg'


def test_relative_name_falls_back_to_bare_name_when_no_folder_set(clean_state, tmp_path):
    assert state['folder'] is None
    nested = tmp_path / 'session1' / 'photo.jpg'
    assert _relative_name(nested) == 'photo.jpg'


def test_relative_name_falls_back_when_file_is_outside_watched_folder(clean_state, tmp_path):
    _set_folder(tmp_path / 'watched')
    outside = tmp_path / 'elsewhere' / 'photo.jpg'
    assert _relative_name(outside) == 'photo.jpg'


# ---------------------------------------------------------------------------
# process_file — filename identity includes the subfolder
# ---------------------------------------------------------------------------

def test_process_file_filename_includes_subfolder(clean_state, make_jpeg, tmp_path):
    _set_folder(tmp_path)
    path = make_jpeg('session1/photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    assert state['photos'][0]['filename'] == 'session1/photo.jpg'


def test_process_file_same_basename_in_different_subfolders_both_tracked(clean_state, make_jpeg, tmp_path):
    _set_folder(tmp_path)
    p1 = make_jpeg('session1/photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    p2 = make_jpeg('session2/photo.jpg', flash=1, timestamp='2026:01:01 11:00:00')
    process_file(str(p1))
    process_file(str(p2))

    filenames = sorted(p['filename'] for p in state['photos'])
    assert filenames == ['session1/photo.jpg', 'session2/photo.jpg']


# ---------------------------------------------------------------------------
# scan_folder — recursive discovery
# ---------------------------------------------------------------------------

def test_scan_folder_finds_photos_in_subfolders(clean_state, make_jpeg, tmp_path):
    _set_folder(tmp_path)
    make_jpeg('top.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    make_jpeg('session1/nested.jpg', flash=1, timestamp='2026:01:01 10:01:00')
    make_jpeg('session1/sub2/deeper.jpg', flash=1, timestamp='2026:01:01 10:02:00')

    scan_folder(str(tmp_path))

    filenames = sorted(p['filename'] for p in state['photos'])
    assert filenames == ['session1/nested.jpg', 'session1/sub2/deeper.jpg', 'top.jpg']


def test_scan_folder_skips_hidden_directories(clean_state, make_jpeg, tmp_path):
    _set_folder(tmp_path)
    make_jpeg('visible.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    make_jpeg('.hidden/secret.jpg', flash=1, timestamp='2026:01:01 10:01:00')

    scan_folder(str(tmp_path))

    filenames = [p['filename'] for p in state['photos']]
    assert filenames == ['visible.jpg']


# ---------------------------------------------------------------------------
# handle_modified — sidecar matching stays scoped to the same subfolder
# ---------------------------------------------------------------------------

def test_handle_modified_sidecar_only_matches_same_subfolder(clean_state, make_jpeg, tmp_path):
    _set_folder(tmp_path)
    p1 = make_jpeg('session1/photo.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    p2 = make_jpeg('session2/photo.jpg', flash=1, timestamp='2026:01:01 11:00:00')
    process_file(str(p1))
    process_file(str(p2))

    sidecar = p1.with_suffix('.xmp')
    sidecar.write_bytes(b'<xmp:Rating>5</xmp:Rating>')
    handle_modified(str(sidecar))

    by_name = {p['filename']: p for p in state['photos']}
    assert by_name['session1/photo.jpg']['rating'] == 5
    assert by_name['session2/photo.jpg']['rating'] is None
