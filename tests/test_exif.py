"""Tests for EXIF reading and preview extraction via Pillow/rawpy."""

import pytest
from server import extract_preview, get_exif_info


# Parametrised over a representative sample of Flash EXIF values.
# Bit 0 of the value determines whether flash fired (1) or not (0).
@pytest.mark.parametrize('flash_val,expected_fired', [
    (0,  False),   # No flash
    (1,  True),    # Flash fired
    (5,  True),    # Flash fired, return not detected
    (7,  True),    # Flash fired, return detected
    (16, False),   # Did not fire, compulsory mode  (0b00010000)
    (24, False),   # Did not fire, auto mode         (0b00011000)
    (25, True),    # Flash fired, auto mode          (0b00011001)
])
def test_flash_detection(make_jpeg, flash_val, expected_fired):
    path = make_jpeg(f'flash_{flash_val}.jpg', flash=flash_val)
    fired, _ = get_exif_info(path)
    assert fired == expected_fired, f'Flash={flash_val}: expected fired={expected_fired}, got {fired}'


def test_timestamp_returned(make_jpeg):
    path = make_jpeg('ts_test.jpg', flash=1, timestamp='2026:05:07 14:30:00')
    _, ts = get_exif_info(path)
    assert ts == '2026:05:07 14:30:00'


def test_missing_file_returns_none(tmp_path):
    flash, ts = get_exif_info(tmp_path / 'nonexistent.jpg')
    assert flash is None
    assert ts is None


def test_extract_preview_jpeg_returns_cached_thumb(make_jpeg):
    path = make_jpeg('preview_test.jpg', flash=1)
    result = extract_preview(path)
    assert result is not None
    assert result != path
    assert result.suffix == '.jpg'
    assert result.exists()
