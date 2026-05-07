"""Flask API tests — use the test client so no real server is started."""

import pytest
from server import process_file


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

def test_status_shape(client):
    r = client.get('/api/status')
    assert r.status_code == 200
    data = r.get_json()
    assert 'folder' in data
    assert 'photo_count' in data


def test_status_no_folder_initially(client):
    data = client.get('/api/status').get_json()
    assert data['folder'] is None
    assert data['photo_count'] == 0


# ---------------------------------------------------------------------------
# /api/watch
# ---------------------------------------------------------------------------

def test_watch_rejects_missing_folder(client):
    r = client.post('/api/watch', json={'folder': '/nonexistent/path/xyz_tether_test'})
    assert r.status_code == 400
    assert 'error' in r.get_json()


def test_watch_accepts_real_folder(client, tmp_path):
    r = client.post('/api/watch', json={'folder': str(tmp_path)})
    assert r.status_code == 200
    data = r.get_json()
    assert data['ok'] is True
    assert data['folder'] == str(tmp_path)


def test_watch_expands_tilde(client, tmp_path, monkeypatch):
    # Patch expanduser so ~ resolves to our tmp_path
    monkeypatch.setattr('server.os.path.expanduser', lambda p: str(tmp_path) if p.startswith('~') else p)
    r = client.post('/api/watch', json={'folder': '~/tether-test'})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /api/series
# ---------------------------------------------------------------------------

def test_series_empty_initially(client):
    r = client.get('/api/series')
    assert r.status_code == 200
    assert r.get_json() == []


def test_series_after_processing_files(client, make_jpeg):
    base = make_jpeg('base.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    ov1  = make_jpeg('ov1.jpg',  flash=0, timestamp='2026:01:01 10:00:01')
    ov2  = make_jpeg('ov2.jpg',  flash=0, timestamp='2026:01:01 10:00:02')

    for f in [base, ov1, ov2]:
        process_file(str(f))

    series = client.get('/api/series').get_json()
    assert len(series) == 1
    assert series[0]['base']['filename'] == 'base.jpg'
    assert [o['filename'] for o in series[0]['overlays']] == ['ov1.jpg', 'ov2.jpg']


def test_series_flash_field_present(client, make_jpeg):
    process_file(str(make_jpeg('base.jpg', flash=1, timestamp='2026:01:01 10:00:00')))
    series = client.get('/api/series').get_json()
    assert series[0]['base']['flash'] is True


def test_duplicate_file_not_added_twice(client, make_jpeg):
    path = make_jpeg('base.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))
    process_file(str(path))   # second call should be a no-op
    series = client.get('/api/series').get_json()
    assert len(series) == 1


# ---------------------------------------------------------------------------
# /api/preview
# ---------------------------------------------------------------------------

def test_preview_unknown_filename_returns_404(client):
    assert client.get('/api/preview/no_such_file.jpg').status_code == 404


def test_preview_jpeg_returns_image(client, make_jpeg):
    path = make_jpeg('preview.jpg', flash=1, timestamp='2026:01:01 10:00:00')
    process_file(str(path))

    r = client.get('/api/preview/preview.jpg')
    assert r.status_code == 200
    assert r.content_type.startswith('image/')
