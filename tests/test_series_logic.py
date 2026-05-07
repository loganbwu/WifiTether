"""Unit tests for compute_series — no files or external tools needed."""

import pytest
from server import compute_series


def photo(filename, flash, timestamp='2026:01:01 10:00:00'):
    return {'filename': filename, 'path': '', 'flash': flash, 'timestamp': timestamp, 'has_preview': False}


def test_empty():
    assert compute_series([]) == []


def test_single_base():
    result = compute_series([photo('base.jpg', True)])
    assert len(result) == 1
    assert result[0]['base']['filename'] == 'base.jpg'
    assert result[0]['overlays'] == []


def test_base_with_overlays():
    photos = [
        photo('base.jpg',  True,  '2026:01:01 10:00:00'),
        photo('ov1.jpg',   False, '2026:01:01 10:00:01'),
        photo('ov2.jpg',   False, '2026:01:01 10:00:02'),
    ]
    result = compute_series(photos)
    assert len(result) == 1
    assert result[0]['base']['filename'] == 'base.jpg'
    assert [o['filename'] for o in result[0]['overlays']] == ['ov1.jpg', 'ov2.jpg']


def test_multiple_series():
    photos = [
        photo('base1.jpg', True,  '2026:01:01 10:00:00'),
        photo('ov1.jpg',   False, '2026:01:01 10:00:01'),
        photo('base2.jpg', True,  '2026:01:01 10:01:00'),
        photo('ov2.jpg',   False, '2026:01:01 10:01:01'),
        photo('ov3.jpg',   False, '2026:01:01 10:01:02'),
    ]
    result = compute_series(photos)
    assert len(result) == 2
    assert result[0]['base']['filename'] == 'base1.jpg'
    assert [o['filename'] for o in result[0]['overlays']] == ['ov1.jpg']
    assert result[1]['base']['filename'] == 'base2.jpg'
    assert [o['filename'] for o in result[1]['overlays']] == ['ov2.jpg', 'ov3.jpg']


def test_consecutive_bases_each_get_their_own_series():
    photos = [
        photo('base1.jpg', True, '2026:01:01 10:00:00'),
        photo('base2.jpg', True, '2026:01:01 10:01:00'),
    ]
    result = compute_series(photos)
    assert len(result) == 2
    assert result[0]['overlays'] == []
    assert result[1]['overlays'] == []


def test_orphaned_overlay_before_any_base_is_ignored():
    result = compute_series([photo('ov.jpg', False)])
    assert result == []


def test_overlay_goes_to_most_recent_base():
    # ov2 should attach to base2, not base1
    photos = [
        photo('base1.jpg', True,  '2026:01:01 10:00:00'),
        photo('base2.jpg', True,  '2026:01:01 10:01:00'),
        photo('ov1.jpg',   False, '2026:01:01 10:01:01'),
    ]
    result = compute_series(photos)
    assert result[0]['overlays'] == []
    assert result[1]['overlays'][0]['filename'] == 'ov1.jpg'
