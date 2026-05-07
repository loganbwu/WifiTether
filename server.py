#!/usr/bin/env python3
"""
Tethering viewer — local Flask server.

Usage:
    pip install -r requirements.txt
    python server.py
    # Open http://localhost:5001

Requires exiftool to be installed:
    brew install exiftool
"""

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
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


def check_exiftool() -> bool:
    try:
        r = subprocess.run(['exiftool', '-ver'], capture_output=True, timeout=5)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def get_exif_info(filepath: Path) -> tuple[bool | None, str | None]:
    """Return (flash_fired, timestamp_str). Returns (None, None) on failure."""
    try:
        r = subprocess.run(
            ['exiftool', '-json', '-n', '-Flash', '-DateTimeOriginal', str(filepath)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None, None
        data = json.loads(r.stdout)
        if not data:
            return None, None
        exif = data[0]
        flash_raw = exif.get('Flash')
        # Bit 0: flash fired (1) or not (0)
        flash_fired = bool(int(flash_raw) & 0x01) if flash_raw is not None else False
        timestamp = exif.get('DateTimeOriginal', '')
        return flash_fired, str(timestamp)
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

    for tag in ['-JpgFromRaw', '-PreviewImage']:
        try:
            r = subprocess.run(
                ['exiftool', '-b', tag, str(filepath)],
                capture_output=True, timeout=20,
            )
            if r.returncode == 0 and len(r.stdout) > 1000:
                cache_path.write_bytes(r.stdout)
                return cache_path
        except Exception:
            pass

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
        'exiftool': check_exiftool(),
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
    if not check_exiftool():
        print('WARNING: exiftool not found. Install with: brew install exiftool')
    print('Tethering viewer: http://localhost:5001')
    app.run(port=5001, debug=False, threaded=True)
