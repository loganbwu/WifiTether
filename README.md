# WifiTether

A local-only utility for reviewing shots in real time while tethering. It has two tools that work together:

- **FTP server** (`rye run ftp`): receives photos sent over wifi from the camera (e.g. Canon EOS R3) and saves them to a folder.
- **Viewer** (`rye run wifitether`): watches that folder in the browser. It detects flash vs. no-flash shots as they arrive, groups them into series, and composites them with screen blending.

The viewer works with any folder, so you can also use it without the FTP server (e.g. with USB tethering or a card reader).

**Requirements:**
- Python 3.12+
- [rye](https://rye.astral.sh/)
- For wifi upload: the Mac and camera on the same private network (e.g. your phone's hotspot)

**Setup (once):**
```bash
rye sync
```

## Usage

Both tools default to today's shoot folder, `~/Pictures/YYYY/YYYY-MM-DD`, so with no arguments the camera uploads to the folder the viewer suggests.

**1. Start the FTP server** (terminal 1):
```bash
rye run ftp
```
It creates today's folder if needed and prints something like:
```
   Host:          172.20.10.2
   Port:          2121
   Passive ports: 60000-60099
   Upload folder: /Users/you/Pictures/2026/2026-05-07
   Username:      anonymous (no password)
```
Start this first: it creates the folder, and the viewer can't watch a folder that doesn't exist yet.

**2. Point the camera at it.** In the camera's FTP transfer settings (menu names vary by model), set:
- Server address: the **Host** IP printed above
- Port: **2121**
- Passive mode: **on**
- Login: **anonymous**
- Proxy: off; target folder: root

Turn on automatic transfer after shooting if you want every shot sent as you take it. The Mac's IP address can change when you rejoin a network, so check the printed Host each session.

**3. Start the viewer** (terminal 2):
```bash
rye run wifitether
```
This opens **http://localhost:5001**. The folder box already shows today's folder. Click **Watch**, and new shots appear as they arrive. Any subfolders the camera creates are picked up too.

**Stopping:** press Ctrl+C in each terminal. If the FTP server says the port is already in use, an old copy is still running. Stop it with `pkill -f ftp_server.py`.

**Options:**
```bash
rye run ftp --dir ~/Pictures/some-other-folder   # upload somewhere else
rye run ftp --port 2122                          # use a different port
```
If you change `--dir`, enter the same folder in the viewer's folder box, or click **Browse** to choose it.

**Security:** the FTP server lets anyone on the network log in without a password and upload, change or delete files in the upload folder. Only run it on a private network you control, never on shared or public wifi.

## Viewer details

**How it works:**
- Photos taken **with flash** are treated as base photos.
- Photos taken **without flash** are treated as overlays, associated with the most recent base.
- The gallery shows one card per series: the base photo's thumbnail is a screen-blended composite of the base and all its overlays, with a badge showing the overlay count.
- The watched folder is scanned recursively, so photos in subfolders (e.g. per-session folders) are picked up too. Two subfolders can each have their own same-named file (e.g. `session1/IMG_0001.CR3` and `session2/IMG_0001.CR3`) without colliding — hidden dot-directories are skipped.
- Clicking a card opens the multiple exposure viewer for that series, where individual overlays can be toggled on and off.
- New files are detected automatically — no need to refresh.
- If the viewer is open when a new photo arrives, it automatically jumps to the most recent overlay.

**Flash detection overrides:**
Some flashes (e.g. off-camera/wireless triggers) never get recorded in the camera's own EXIF Flash tag, which can misclassify a base photo as an overlay. Add a `flash_fired` or `flash_not_fired` keyword (e.g. in Lightroom) to override the detected value for a photo — picked up live via a sidecar `.xmp` or embedded XMP, no restart needed.

**Star ratings:**
- The gallery can be filtered to series whose base photo is rated N stars and up, using the star row above the gallery.
- Ratings are read from a Lightroom catalog (`.lrcat`) if you point the app at one, falling back to a sidecar `.xmp` or embedded XMP otherwise.
- Rating changes made after a photo is loaded are picked up automatically — sidecar/embedded XMP edits are detected instantly, and the Lightroom catalog is polled every 5 seconds.

## Testing
```bash
rye run test
```
