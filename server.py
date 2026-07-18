#!/usr/bin/env python3
"""
Tethering viewer — local Flask server.

Usage:
    rye sync
    rye run wifitether
    # Open http://localhost:5001
"""

import functools
import io
import json
import os
import queue
import re
import sqlite3
import hashlib
import struct
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rawpy
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from PIL import Image, ImageChops, ImageOps
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

PREVIEW_CACHE_DIR = Path('/tmp/tether_previews')
PREVIEW_CACHE_DIR.mkdir(exist_ok=True)
THUMB_MAX_PX = 1600

RAW_EXTENSIONS = {'.cr3', '.cr2', '.nef', '.arw', '.raf', '.dng'}
JPEG_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
IMG_EXTENSIONS = RAW_EXTENSIONS | JPEG_EXTENSIONS

state_lock = threading.Lock()
state = {
    'folder': None,
    'photos': [],   # [{filename, path, flash, timestamp}] sorted by timestamp
    'series': [],   # [{base: photo, overlays: [photo, ...]}]
    'lightroom_catalog': None,   # path to a .lrcat, or None
}

LIGHTROOM_POLL_INTERVAL_SEC = 5

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


# xmp:Rating can appear as either an attribute (xmp:Rating="3") or an
# element (<xmp:Rating>3</xmp:Rating>) — Adobe products use -1 for "rejected".
_XMP_RATING_RE = re.compile(rb'xmp:Rating(?:\s*=\s*"(-?\d+)"|>(-?\d+)<)')


def _rating_from_xmp_bytes(data: bytes) -> int | None:
    """Find an xmp:Rating value anywhere in a blob that may embed an XMP packet."""
    match = _XMP_RATING_RE.search(data)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def get_file_rating(filepath: Path) -> int | None:
    """Return a photo's star rating, checking a sidecar .xmp first, then XMP
    embedded in the file itself. Returns None if neither has a rating."""
    sidecar = filepath.with_suffix('.xmp')
    if sidecar.exists():
        try:
            rating = _rating_from_xmp_bytes(sidecar.read_bytes())
            if rating is not None:
                return rating
        except Exception as e:
            print(f'Sidecar XMP error for {sidecar.name}: {e}')
    try:
        return _rating_from_xmp_bytes(filepath.read_bytes())
    except Exception as e:
        print(f'Embedded XMP error for {filepath.name}: {e}')
        return None


# A Lightroom keyword lets a photographer manually correct flash detection —
# e.g. an off-camera/wireless-triggered flash that the camera's own EXIF Flash
# tag never records as having fired. Keywords round-trip through dc:subject
# in the same sidecar/embedded XMP we already scan for ratings.
_FLASH_KEYWORD_RE = re.compile(rb'<rdf:li>\s*(flash_fired|flash_not_fired)\s*</rdf:li>', re.IGNORECASE)


def _flash_override_from_xmp_bytes(data: bytes) -> bool | None:
    """Find a 'flash_fired'/'flash_not_fired' keyword in an XMP dc:subject list.
    Returns None if neither keyword is present, or if both are (ambiguous)."""
    keywords = {m.group(1).lower() for m in _FLASH_KEYWORD_RE.finditer(data)}
    fired = b'flash_fired' in keywords
    not_fired = b'flash_not_fired' in keywords
    if fired == not_fired:   # neither present, or both (contradictory) — don't guess
        return None
    return fired


def get_flash_override(filepath: Path) -> bool | None:
    """Return an explicit flash-fired override from a Lightroom keyword,
    checking a sidecar .xmp first, then XMP embedded in the file itself.
    Returns None if no override keyword is present."""
    sidecar = filepath.with_suffix('.xmp')
    if sidecar.exists():
        try:
            override = _flash_override_from_xmp_bytes(sidecar.read_bytes())
            if override is not None:
                return override
        except Exception as e:
            print(f'Sidecar XMP flash-override error for {sidecar.name}: {e}')
    try:
        return _flash_override_from_xmp_bytes(filepath.read_bytes())
    except Exception as e:
        print(f'Embedded XMP flash-override error for {filepath.name}: {e}')
        return None


def query_lightroom_ratings(catalog_path: str, filenames: list) -> dict:
    """Batch-lookup star ratings from a Lightroom catalog for the given filenames.

    The Adobe_images/AgLibraryFile schema is unofficial and reverse-engineered,
    so any failure here (locked file, unexpected schema, missing catalog) is
    swallowed and reported as 'nothing found' — callers fall back to file-based
    ratings rather than breaking the scan.
    """
    if not filenames:
        return {}
    lower_to_name = {f.lower(): f for f in filenames}
    placeholders = ','.join('?' * len(lower_to_name))
    query = f"""
        SELECT LOWER(f.baseName || '.' || f.extension) AS fname, i.rating
        FROM Adobe_images i
        JOIN AgLibraryFile f ON f.id_local = i.rootFile
        WHERE LOWER(f.baseName || '.' || f.extension) IN ({placeholders})
    """
    try:
        with sqlite3.connect(f'file:{catalog_path}?mode=ro', uri=True, timeout=2) as conn:
            rows = conn.execute(query, list(lower_to_name)).fetchall()
    except Exception as e:
        print(f'Lightroom catalog read error: {e}')
        return {}
    return {
        lower_to_name[fname]: int(rating)
        for fname, rating in rows
        if rating is not None and fname in lower_to_name
    }


def get_rating(filepath: Path, catalog_path: str | None) -> int | None:
    """Resolve a photo's rating: Lightroom catalog first (if configured), then
    sidecar/embedded XMP as a fallback."""
    if catalog_path:
        catalog_rating = query_lightroom_ratings(catalog_path, [filepath.name]).get(filepath.name)
        if catalog_rating is not None:
            return catalog_rating
    return get_file_rating(filepath)


def _open_rotated(filepath: Path) -> Image.Image | None:
    """Open an image and apply EXIF orientation, returning an RGB-ready Image."""
    if filepath.suffix.lower() in JPEG_EXTENSIONS:
        return ImageOps.exif_transpose(Image.open(filepath))
    with rawpy.imread(str(filepath)) as raw:
        thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            return ImageOps.exif_transpose(Image.open(io.BytesIO(bytes(thumb.data))))
    return None


def _preview_cache_key(filepath: Path) -> str:
    """A cache key that changes if a file's location or content changes, so a
    re-exported/edited file (even reusing an original's filename, e.g. a
    Lightroom JPEG export named after its source CR3) doesn't reuse a stale
    cached preview rendered from a different file."""
    try:
        stat = filepath.stat()
        sig = f'{filepath.resolve()}:{stat.st_mtime_ns}:{stat.st_size}'
    except OSError:
        sig = str(filepath.resolve())
    return hashlib.sha1(sig.encode()).hexdigest()


def extract_preview(filepath: Path) -> Path | None:
    """Return path to a resized JPEG thumbnail. Returns None if extraction fails."""
    cache_path = PREVIEW_CACHE_DIR / (_preview_cache_key(filepath) + '_preview.jpg')
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


def composite_series_preview(entry: dict) -> Path | None:
    """Return path to a thumbnail of the base screen-blended with its overlays.
    Falls back to the plain base preview when there are no overlays."""
    base = entry['base']
    overlays = entry['overlays']
    if not overlays:
        return extract_preview(Path(base['path']))

    combined_sig = _preview_cache_key(Path(base['path'])) + ''.join(
        _preview_cache_key(Path(overlay['path'])) for overlay in overlays
    )
    cache_key = hashlib.sha1(combined_sig.encode()).hexdigest()
    cache_path = PREVIEW_CACHE_DIR / (cache_key + '_composite.jpg')
    if cache_path.exists():
        return cache_path

    base_preview = extract_preview(Path(base['path']))
    if not base_preview:
        return None
    try:
        img = Image.open(base_preview).convert('RGB')
        for overlay in overlays:
            overlay_preview = extract_preview(Path(overlay['path']))
            if not overlay_preview:
                continue
            overlay_img = Image.open(overlay_preview).convert('RGB').resize(img.size, Image.LANCZOS)
            img = ImageChops.screen(img, overlay_img)
        img.save(cache_path, 'JPEG', quality=85)
        return cache_path
    except Exception as e:
        print(f'Composite preview error for {base["filename"]}: {e}')
        return None


def extract_full(filepath: Path) -> Path | None:
    """Return path to a full-resolution rotation-corrected JPEG. Returns None on failure."""
    cache_path = PREVIEW_CACHE_DIR / (_preview_cache_key(filepath) + '_full.jpg')
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

    exif_flash = flash
    flash_override = get_flash_override(filepath)
    if flash_override is not None:
        flash = flash_override

    preview_path = extract_preview(filepath)

    aspect = None
    if preview_path:
        try:
            with Image.open(preview_path) as img:
                w, h = img.size
                aspect = round(w / h, 4) if h else None
        except Exception:
            pass

    with state_lock:
        catalog = state['lightroom_catalog']
    rating = get_rating(filepath, catalog)

    photo = {
        'filename': filepath.name,
        'path': str(filepath),
        'flash': flash,
        'exif_flash': exif_flash,
        'timestamp': timestamp or '',
        'has_preview': preview_path is not None,
        'aspect': aspect,
        'error': error,
        'rating': rating,
        'mtime': filepath.stat().st_mtime,
    }

    with state_lock:
        if any(p['filename'] == filepath.name for p in state['photos']):
            return
        state['photos'].append(photo)
        state['photos'].sort(key=lambda x: x['timestamp'])
        state['series'] = compute_series(state['photos'])

    notify_clients({'type': 'update', 'filename': filepath.name})
    print(f'Added: {filepath.name}  flash={flash}  ts={timestamp}  rating={rating}')


def refresh_metadata(filename: str, full_rescan: bool = False) -> None:
    """Re-derive a single already-tracked photo's metadata and notify clients
    if anything changed.

    full_rescan=True re-derives everything process_file would (EXIF flash/
    timestamp, preview/aspect, rating, flash override) -- used when the photo
    file itself was rewritten, e.g. re-exported from Lightroom after further
    edits, since the pixel content and embedded metadata could both differ.
    full_rescan=False (the default) only re-checks rating and the flash
    override, which is all a sidecar .xmp change on its own could affect.
    """
    with state_lock:
        photo = next((p for p in state['photos'] if p['filename'] == filename), None)
        catalog = state['lightroom_catalog']
    if not photo:
        return

    filepath = Path(photo['path'])
    if not filepath.is_file():
        return

    updated = {}
    if full_rescan:
        flash, timestamp = get_exif_info(filepath)
        error = None
        if flash is None:
            flash = False
            error = 'Could not read EXIF'
        exif_flash = flash
        flash_override = get_flash_override(filepath)
        flash = flash_override if flash_override is not None else exif_flash

        preview_path = extract_preview(filepath)
        aspect = None
        if preview_path:
            try:
                with Image.open(preview_path) as img:
                    w, h = img.size
                    aspect = round(w / h, 4) if h else None
            except Exception:
                pass

        updated.update({
            'flash': flash,
            'exif_flash': exif_flash,
            'timestamp': timestamp or '',
            'has_preview': preview_path is not None,
            'aspect': aspect,
            'error': error,
            'mtime': filepath.stat().st_mtime,
        })
    else:
        flash_override = get_flash_override(filepath)
        updated['flash'] = flash_override if flash_override is not None else photo['exif_flash']

    updated['rating'] = get_rating(filepath, catalog)

    with state_lock:
        if all(photo.get(k) == v for k, v in updated.items()):
            return
        photo.update(updated)
        if full_rescan:
            state['photos'].sort(key=lambda x: x['timestamp'])
        state['series'] = compute_series(state['photos'])

    notify_clients({'type': 'metadata_updated'})
    print(f'Metadata refreshed: {filename} -> {updated}')


def handle_modified(filepath_str: str) -> None:
    """React to a sidecar .xmp being written, or a photo file itself being
    rewritten (e.g. re-exported from Lightroom after further edits)."""
    filepath = Path(filepath_str)
    if not filepath.is_file():
        return

    with state_lock:
        known_filenames = {p['filename'] for p in state['photos']}

    if filepath.suffix.lower() == '.xmp':
        matched = next((name for name in known_filenames if Path(name).stem == filepath.stem), None)
        full_rescan = False
    elif filepath.name in known_filenames:
        matched = filepath.name
        full_rescan = True
    else:
        matched = None
        full_rescan = False

    if matched:
        wait_for_file_stable(filepath, timeout=5)
        refresh_metadata(matched, full_rescan=full_rescan)


def lightroom_poll_loop() -> None:
    """Periodically re-check the Lightroom catalog for rating changes.

    Filesystem events aren't reliable for the catalog itself (in WAL mode the
    main .lrcat file's mtime may not change until a checkpoint), so this polls
    instead of watching.
    """
    while True:
        time.sleep(LIGHTROOM_POLL_INTERVAL_SEC)
        with state_lock:
            catalog = state['lightroom_catalog']
            filenames = [p['filename'] for p in state['photos']]
        if not catalog or not filenames:
            continue

        ratings = query_lightroom_ratings(catalog, filenames)
        changed = False
        with state_lock:
            for photo in state['photos']:
                new_rating = ratings.get(photo['filename'])
                if new_rating is not None and photo['rating'] != new_rating:
                    photo['rating'] = new_rating
                    changed = True
            if changed:
                state['series'] = compute_series(state['photos'])
        if changed:
            notify_clients({'type': 'metadata_updated'})


class FolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if Path(event.src_path).suffix.lower() == '.xmp':
            threading.Thread(target=handle_modified, args=(event.src_path,), daemon=True).start()
        else:
            threading.Thread(target=process_file, args=(event.src_path,), daemon=True).start()

    def on_modified(self, event):
        if not event.is_directory:
            threading.Thread(target=handle_modified, args=(event.src_path,), daemon=True).start()


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


threading.Thread(target=lightroom_poll_loop, daemon=True).start()


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
        lightroom_catalog = state['lightroom_catalog']
    return jsonify({
        'folder': folder,
        'photo_count': photo_count,
        'lightroom_catalog': lightroom_catalog,
    })


@app.route('/api/lightroom-catalog', methods=['POST'])
def api_lightroom_catalog():
    data = request.get_json(force=True)
    path = os.path.expanduser((data.get('path') or '').strip())

    if path and not os.path.isfile(path):
        return jsonify({'error': f'Not a file: {path}'}), 400

    with state_lock:
        state['lightroom_catalog'] = path or None
    return jsonify({'ok': True, 'lightroom_catalog': state['lightroom_catalog']})


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
    min_rating = request.args.get('min_rating', type=int)
    with state_lock:
        series = state['series']
        if min_rating:
            series = [s for s in series if (s['base'].get('rating') or 0) >= min_rating]
        return jsonify(series)


@app.route('/api/preview/<path:filename>')
def api_preview(filename: str):
    with state_lock:
        photo = next((p for p in state['photos'] if p['filename'] == filename), None)
        entry = next((s for s in state['series'] if s['base']['filename'] == filename), None)
    if not photo:
        return 'Not found', 404
    preview = composite_series_preview(entry) if entry else extract_preview(Path(photo['path']))
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
    threading.Timer(1.0, lambda: webbrowser.open('http://localhost:5001')).start()
    app.run(port=5001, debug=False, threaded=True)
