#!/usr/bin/env python3
"""
Tethering viewer — local Flask server.

Usage:
    rye sync
    rye run start
    # Open http://localhost:5001
"""

import functools
import io
import json
import os
import queue
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rawpy
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from PIL import Image, ImageOps
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

PREVIEW_CACHE_DIR = Path('/tmp/tether_previews')
PREVIEW_CACHE_DIR.mkdir(exist_ok=True)
THUMB_MAX_PX = 800

RAW_EXTENSIONS = {'.cr3', '.cr2', '.nef', '.arw', '.raf', '.dng'}
JPEG_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
IMG_EXTENSIONS = RAW_EXTENSIONS | JPEG_EXTENSIONS

state_lock = threading.Lock()
state = {
    'folder': None,
    'photos': [],   # [{filename, path, flash, timestamp}] sorted by timestamp
    'series': [],   # [{base: photo, overlays: [photo, ...]}]
}

sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()
observer: Observer | None = None
observer_lock = threading.Lock()


_FLASH_TAG = 37385   # ExifIFD.Flash
_DTO_TAG   = 36867   # ExifIFD.DateTimeOriginal
_EXIF_IFD  = 0x8769  # IFD0 pointer tag for ExifIFD sub-IFD

# Canon CR3 uses ISOBMFF. The ExifIFD lives in a CMT2 sub-box inside a
# uuid box (UUID 85c0b687...) inside the moov box.
_CANON_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')


def _parse_flash_dto(flash_raw, dto_raw) -> tuple[bool | None, str | None]:
    flash = bool(int(flash_raw) & 0x01) if flash_raw is not None else None
    return flash, str(dto_raw) if dto_raw else None


def _exif_from_image(img: Image.Image) -> tuple[bool | None, str | None]:
    """Read Flash and DateTimeOriginal from an open Pillow image."""
    ifd = img.getexif().get_ifd(_EXIF_IFD)
    return _parse_flash_dto(ifd.get(_FLASH_TAG), ifd.get(_DTO_TAG))


def _read_tiff_tag(tiff: bytes, tag: int) -> 'int | str | None':
    """Return the value of a tag from a raw TIFF IFD block."""
    if len(tiff) < 8:
        return None
    endian = '<' if tiff[:2] == b'II' else '>'
    ifd_off = struct.unpack_from(endian + 'I', tiff, 4)[0]
    n = struct.unpack_from(endian + 'H', tiff, ifd_off)[0]
    for i in range(n):
        off = ifd_off + 2 + i * 12
        if off + 12 > len(tiff):
            break
        t, typ, count = struct.unpack_from(endian + 'HHI', tiff, off)
        if t != tag:
            continue
        raw = tiff[off + 8:off + 12]
        if typ == 3 and count == 1:   # SHORT — inline
            return struct.unpack_from(endian + 'H', raw)[0]
        if typ == 4 and count == 1:   # LONG — inline
            return struct.unpack_from(endian + 'I', raw)[0]
        if typ == 2:                  # ASCII
            if count > 4:
                val_off = struct.unpack_from(endian + 'I', raw)[0]
                return tiff[val_off:val_off + count].rstrip(b'\x00').decode('ascii', 'replace')
            return raw[:count].rstrip(b'\x00').decode('ascii', 'replace')
    return None


def _cr3_cmt2(data: bytes) -> 'bytes | None':
    """Extract the CMT2 (ExifIFD) box payload from a Canon CR3 file's bytes."""
    def iter_boxes(buf: bytes, start: int, end: int):
        off = start
        while off + 8 <= end:
            size = struct.unpack_from('>I', buf, off)[0]
            btype = buf[off + 4:off + 8]
            payload = off + 8
            if size == 1:
                size = struct.unpack_from('>Q', buf, off + 8)[0]
                payload = off + 16
            if size == 0:
                size = end - off
            yield btype, payload, off + size
            off += size

    moov_start = moov_end = None
    for btype, s, e in iter_boxes(data, 0, len(data)):
        if btype == b'moov':
            moov_start, moov_end = s, e
            break
    if moov_start is None:
        return None

    for btype, s, e in iter_boxes(data, moov_start, moov_end):
        if btype == b'uuid' and data[s:s + 16] == _CANON_UUID:
            for btype2, s2, e2 in iter_boxes(data, s + 16, e):
                if btype2 == b'CMT2':
                    return data[s2:e2]
    return None


def get_exif_info(filepath: Path) -> tuple[bool | None, str | None]:
    """Return (flash_fired, timestamp_str). Returns (None, None) on failure."""
    try:
        if filepath.suffix.lower() in JPEG_EXTENSIONS:
            with Image.open(filepath) as img:
                return _exif_from_image(img)
        # Raw/CR3: parse EXIF directly from the ISOBMFF CMT2 box
        cmt2 = _cr3_cmt2(filepath.read_bytes())
        if cmt2 is not None:
            return _parse_flash_dto(_read_tiff_tag(cmt2, _FLASH_TAG), _read_tiff_tag(cmt2, _DTO_TAG))
    except Exception as e:
        print(f'EXIF error for {filepath.name}: {e}')
    return None, None


def _open_rotated(filepath: Path) -> Image.Image | None:
    """Open an image and apply EXIF orientation, returning an RGB-ready Image."""
    if filepath.suffix.lower() in JPEG_EXTENSIONS:
        return ImageOps.exif_transpose(Image.open(filepath))
    with rawpy.imread(str(filepath)) as raw:
        thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            return ImageOps.exif_transpose(Image.open(io.BytesIO(bytes(thumb.data))))
    return None


def extract_preview(filepath: Path) -> Path | None:
    """Return path to a resized JPEG thumbnail. Returns None if extraction fails."""
    cache_path = PREVIEW_CACHE_DIR / (filepath.stem + '_preview.jpg')
    if cache_path.exists():
        return cache_path
    try:
        img = _open_rotated(filepath)
        if img is not None:
            img = img.convert('RGB')
            img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
            img.save(cache_path, 'JPEG', quality=85)
            return cache_path
    except Exception as e:
        print(f'Preview extraction error for {filepath.name}: {e}')
    return None


def extract_full(filepath: Path) -> Path | None:
    """Return path to a full-resolution rotation-corrected JPEG. Returns None on failure."""
    cache_path = PREVIEW_CACHE_DIR / (filepath.stem + '_full.jpg')
    if cache_path.exists():
        return cache_path
    try:
        img = _open_rotated(filepath)
        if img is not None:
            img.convert('RGB').save(cache_path, 'JPEG', quality=95)
            return cache_path
    except Exception as e:
        print(f'Full extraction error for {filepath.name}: {e}')
    return None


def wait_for_file_stable(filepath: Path, timeout: int = 15) -> bool:
    """Wait until file size stops growing (file fully written by camera)."""
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        try:
            size = filepath.stat().st_size
            if size == last_size and size > 0:
                return True
            last_size = size
        except OSError:
            pass
        time.sleep(0.5)
    return False


def compute_series(photos: list) -> list:
    has_flash = any(not p.get('error') and p['flash'] for p in photos)

    if not has_flash:
        return [{'base': p, 'overlays': []} for p in photos]

    series = []
    last_flash_entry = None
    for photo in photos:
        if photo.get('error'):
            series.append({'base': photo, 'overlays': []})
        elif photo['flash']:
            entry = {'base': photo, 'overlays': []}
            series.append(entry)
            last_flash_entry = entry
        elif last_flash_entry is not None:
            last_flash_entry['overlays'].append(photo)
    return series


def process_file(filepath_str: str, skip_stability_check: bool = False) -> None:
    filepath = Path(filepath_str)
    if filepath.suffix.lower() not in IMG_EXTENSIONS:
        return
    if not filepath.is_file():
        return

    if not skip_stability_check and not wait_for_file_stable(filepath):
        print(f'File did not stabilise: {filepath.name}')
        return

    flash, timestamp = get_exif_info(filepath)
    error = None
    if flash is None:
        print(f'Could not read EXIF from {filepath.name}, adding as placeholder')
        flash = False
        error = 'Could not read EXIF'

    preview_path = extract_preview(filepath)

    aspect = None
    if preview_path:
        try:
            with Image.open(preview_path) as img:
                w, h = img.size
                aspect = round(w / h, 4) if h else None
        except Exception:
            pass

    photo = {
        'filename': filepath.name,
        'path': str(filepath),
        'flash': flash,
        'timestamp': timestamp or '',
        'has_preview': preview_path is not None,
        'aspect': aspect,
        'error': error,
    }

    with state_lock:
        if any(p['filename'] == filepath.name for p in state['photos']):
            return
        state['photos'].append(photo)
        state['photos'].sort(key=lambda x: x['timestamp'])
        state['series'] = compute_series(state['photos'])

    notify_clients({'type': 'update', 'filename': filepath.name})
    print(f'Added: {filepath.name}  flash={flash}  ts={timestamp}')


class FolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.src_path,), daemon=True).start()


def notify_clients(event: dict) -> None:
    data = 'data: ' + json.dumps(event) + '\n\n'
    with sse_lock:
        for q in list(sse_clients):
            q.put(data)


def scan_folder(folder: str) -> None:
    """Process existing files in the folder in parallel (files are already fully written)."""
    files = sorted(Path(folder).iterdir(), key=lambda f: f.stat().st_mtime if f.is_file() else 0)
    targets = [str(f) for f in files if f.is_file() and f.suffix.lower() in IMG_EXTENSIONS]
    with ThreadPoolExecutor() as pool:
        pool.map(functools.partial(process_file, skip_stability_check=True), targets)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory('.', 'viewer.html')


@app.route('/api/pick-folder')
def api_pick_folder():
    result = subprocess.run(
        ['osascript', '-e', 'choose folder with prompt "Select a folder to watch"'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({'path': None})
    raw = result.stdout.strip()
    # osascript returns an alias like "Macintosh HD:Users:foo:bar:"
    parts = raw.split(':')
    path = '/' + '/'.join(p for p in parts[1:] if p)
    return jsonify({'path': path})


@app.route('/api/status')
def api_status():
    with state_lock:
        folder = state['folder']
        photo_count = len(state['photos'])
    return jsonify({
        'folder': folder,
        'photo_count': photo_count,
    })


@app.route('/api/watch', methods=['POST'])
def api_watch():
    global observer
    data = request.get_json(force=True)
    folder = os.path.expanduser(data.get('folder', '').strip())

    if not os.path.isdir(folder):
        return jsonify({'error': f'Not a directory: {folder}'}), 400

    with state_lock:
        state['folder'] = folder
        state['photos'] = []
        state['series'] = []

    with observer_lock:
        if observer:
            observer.stop()
            observer.join()
        new_observer = Observer()
        new_observer.schedule(FolderHandler(), folder, recursive=False)
        new_observer.start()
        observer = new_observer

    threading.Thread(target=scan_folder, args=(folder,), daemon=True).start()
    notify_clients({'type': 'folder_changed', 'folder': folder})
    return jsonify({'ok': True, 'folder': folder})


@app.route('/api/series')
def api_series():
    with state_lock:
        return jsonify(state['series'])


@app.route('/api/preview/<path:filename>')
def api_preview(filename: str):
    with state_lock:
        photo = next((p for p in state['photos'] if p['filename'] == filename), None)
    if not photo:
        return 'Not found', 404
    preview = extract_preview(Path(photo['path']))
    if preview:
        return send_file(str(preview), mimetype='image/jpeg')
    return 'Preview not available', 404


@app.route('/api/photo/<path:filename>')
def api_photo(filename: str):
    with state_lock:
        photo = next((p for p in state['photos'] if p['filename'] == filename), None)
    if not photo:
        return 'Not found', 404
    full = extract_full(Path(photo['path']))
    if full:
        return send_file(str(full), mimetype='image/jpeg')
    return 'Photo not available', 404


@app.route('/api/stream')
def api_stream():
    client_queue: queue.Queue = queue.Queue()
    with sse_lock:
        sse_clients.append(client_queue)

    def generate():
        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    msg = client_queue.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ': keepalive\n\n'
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    print('Tethering viewer: http://localhost:5001')
    app.run(port=5001, debug=False, threaded=True)
