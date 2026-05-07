import io
import sys
from pathlib import Path

import piexif
import pytest
from PIL import Image

# Allow importing server from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app as flask_app
from server import state, state_lock

_PHOTOS_DIR = Path(__file__).parent.parent.parent / 'frontend' / 'photos'
_SAMPLE_JPEG = next(_PHOTOS_DIR.glob('*.jpg'))


@pytest.fixture(scope='session')
def sample_jpeg() -> Path:
    return _SAMPLE_JPEG


@pytest.fixture
def make_jpeg(tmp_path):
    """Factory: create a JPEG with specific Flash and DateTimeOriginal EXIF values."""

    def _make(name: str, flash: int, timestamp: str = '2026:01:01 10:00:00') -> Path:
        dst = tmp_path / name
        exif_bytes = piexif.dump({
            '0th': {},
            'Exif': {
                piexif.ExifIFD.Flash: flash,
                piexif.ExifIFD.DateTimeOriginal: timestamp.encode('ascii'),
            },
            '1st': {}, 'GPS': {}, 'Interop': {},
        })
        with Image.open(_SAMPLE_JPEG) as img:
            img.save(str(dst), exif=exif_bytes)
        return dst

    return _make


@pytest.fixture
def clean_state():
    with state_lock:
        state['folder'] = None
        state['photos'] = []
        state['series'] = []
    yield
    with state_lock:
        state['folder'] = None
        state['photos'] = []
        state['series'] = []


@pytest.fixture
def client(clean_state):
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c
