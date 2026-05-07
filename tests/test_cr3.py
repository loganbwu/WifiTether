"""Tests against real CR3 files. Skipped automatically when no CR3s are available."""

import pytest
from pathlib import Path
from server import get_exif_info, extract_preview

CR3_DIR = Path.home() / 'Pictures' / '2026' / '2026-05-07'
CR3_FILES = sorted(CR3_DIR.glob('*.CR3')) if CR3_DIR.exists() else []

pytestmark = pytest.mark.skipif(not CR3_FILES, reason='No CR3 files found in ~/Pictures/2026/2026-05-07')


@pytest.fixture(scope='module')
def cr3_path():
    return CR3_FILES[0]


def test_cr3_exif_reads_without_error(cr3_path):
    flash, dto = get_exif_info(cr3_path)
    assert flash is not None, 'Flash tag missing from CR3 EXIF'
    assert dto is not None, 'DateTimeOriginal tag missing from CR3 EXIF'


def test_cr3_flash_is_bool(cr3_path):
    flash, _ = get_exif_info(cr3_path)
    assert isinstance(flash, bool)


def test_cr3_timestamp_format(cr3_path):
    _, dto = get_exif_info(cr3_path)
    # DateTimeOriginal format: 'YYYY:MM:DD HH:MM:SS'
    assert len(dto) == 19
    assert dto[4] == ':' and dto[7] == ':' and dto[10] == ' '


def test_cr3_preview_extracted(cr3_path, tmp_path):
    preview = extract_preview(cr3_path)
    assert preview is not None, 'Could not extract preview from CR3'
    assert preview.exists()
    assert preview.stat().st_size > 10_000  # sanity: > 10 KB
