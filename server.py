#!/usr/bin/env python3
"""
Tethering viewer — local Flask server.

Usage:
    rye sync
    rye run start
    # Open http://localhost:5001
"""

import io
import json
import os
import queue
import struct
import threading
import time
from pathlib import Path

import rawpy
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

PREVIEW_CACHE_DIR = Path('/tmp/tether_previews')
PREVIEW_CACHE_DIR.mkdir(exist_ok=True)

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


def _exif_from_image(img: Image.Image) -> tuple[bool | None, str | None]:
    """Read Flash and DateTimeOriginal from an open Pillow image."""
    ifd = img.getexif().get_ifd(_EXIF_IFD)
    flash_raw = ifd.get(_FLASH_TAG)
    dto       = ifd.get(_DTO_TAG)
    flash_fired = bool(int(flash_raw) & 0x01) if flash_raw is not None else None
    return flash_fired, str(dto) if dto else None


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
            flash_raw = _read_tiff_tag(cmt2, _FLASH_TAG)
            dto       = _read_tiff_tag(cmt2, _DTO_TAG)
            flash_fired = bool(int(flash_raw) & 0x01) if flash_raw is not None else None
            return flash_fired, str(dto) if dto else None
    except Exception as e:
        print(f'EXIF error for {filepath.name}: {e}')
    return None, None


def extract_preview(filepath: Path) -> Path | None:
    """Return path to a JPEG preview. Returns None if extraction fails."""
    if filepath.suffix.lower() in JPEG_EXTENSIONS:
        return filepath

    cache_path = PREVIEW_CACHE_DIR / (filepath.stem + '_preview.jpg')
    if cache_path.exists():
        return cache_path

    try:
        with rawpy.imread(str(filepath)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                cache_path.write_bytes(bytes(thumb.data))
                return cache_path
    except Exception as e:
        print(f'Preview extraction error for {filepath.name}: {e}')

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
    series = []
    for photo in photos:
        if photo['flash']:
            series.append({'base': photo, 'overlays': []})
        elif series:
            series[-1]['overlays'].append(photo)
    return series


def process_file(filepath_str: str) -> None:
    filepath = Path(filepath_str)
    if filepath.suffix.lower() not in IMG_EXTENSIONS:
        return
    if not filepath.is_file():
        return

    if not wait_for_file_stable(filepath):
        print(f'File did not stabilise: {filepath.name}')
        return

    flash, timestamp = get_exif_info(filepath)
    if flash is None:
        print(f'Could not read EXIF from {filepath.name}, skipping')
        return

    preview_path = extract_preview(filepath)

    photo = {
        'filename': filepath.name,
        'path': str(filepath),
        'flash': flash,
        'timestamp': timestamp or '',
        'has_preview': preview_path is not None,
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
    """Process existing files in the folder, sorted by modification time."""
    files = sorted(Path(folder).iterdir(), key=lambda f: f.stat().st_mtime if f.is_file() else 0)
    for f in files:
        if f.is_file() and f.suffix.lower() in IMG_EXTENSIONS:
            process_file(str(f))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory('.', 'viewer.html')


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
        photos = list(state['photos'])

    photo = next((p for p in photos if p['filename'] == filename), None)
    if not photo:
        return 'Not found', 404

    filepath = Path(photo['path'])

    if filepath.suffix.lower() in JPEG_EXTENSIONS:
        return send_file(str(filepath), mimetype='image/jpeg')

    cache_path = PREVIEW_CACHE_DIR / (filepath.stem + '_preview.jpg')
    if cache_path.exists():
        return send_file(str(cache_path), mimetype='image/jpeg')

    preview = extract_preview(filepath)
    if preview:
        return send_file(str(preview), mimetype='image/jpeg')

    return 'Preview not available', 404


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
