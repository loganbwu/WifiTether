# WifiTether

A local-only utility for reviewing shots in real time while tethering. It has two tools that work together:

- **FTP server** (`rye run ftp`): receives photos sent over wifi from the camera (e.g. Canon EOS R3) and saves them to a folder.
- **Viewer** (`rye run wifitether`): watches that folder in the browser and shows new shots as they arrive. With `--overlays`, it also detects flash vs. no-flash shots, groups them into series, and composites them with screen blending.

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
   Wi-Fi network: My iPhone
   Host:          172.20.10.2
   Port:          2121
   Passive ports: 60000-60099
   Upload folder: /Users/you/Pictures/2026/2026-05-07
   Username:      anonymous (no password)

Connect the camera to My iPhone
Point the camera at 172.20.10.2:2121
```
Start this first: it creates the folder, and the viewer can't watch a folder that doesn't exist yet. Check the Wi-Fi network is the one you expect: if the Mac has joined a different network, the camera won't be able to reach it. If macOS won't reveal the network name, it shows "unknown" and just reminds you to use the same network as the Mac.

**2. Point the camera at it.** 

On a Canon R3, in the menu, go to connection settings > Network settings > Connection settings. If not previously configured, set:
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
rye run wifitether --overlays                    # turn on multiple exposure overlays (see below)
```
If you change `--dir`, enter the same folder in the viewer's folder box, or click **Browse** to choose it.

**Security:** the FTP server lets anyone on the network log in without a password and upload, change or delete files in the upload folder. Only run it on a private network you control, never on shared or public wifi.

## Viewer details

**How it works:**
- By default, every photo gets its own card in the gallery. The overlay behaviour below only applies when the viewer is started with `rye run wifitether --overlays`.
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
- In the viewer, rate the series' base photo with the star buttons or the `1`–`5` keys (`` ` `` or `0` clears it; clicking the current star also clears it). The rating is written to the photo's sidecar `.xmp` (e.g. `IMG_0001.xmp`), created if needed. Other tags already in the sidecar, such as Lightroom edits and keywords, are kept.
- Gallery thumbnails show the base photo's rating as stars in the bottom-left corner.
- The gallery can be filtered to series whose base photo is rated N stars and up, using the star row above the gallery.
- Ratings are read from a Lightroom catalog (`.lrcat`) if you point the app at one, falling back to a sidecar `.xmp` or embedded XMP otherwise. So if a catalog is set and already has a rating for the photo, that rating still wins over one set in the viewer, and the viewer shows a warning. Lightroom only picks up sidecar ratings when you use Metadata > Read Metadata from Files (or when importing).
- Rating changes made after a photo is loaded are picked up automatically — sidecar/embedded XMP edits are detected instantly, and the Lightroom catalog is polled every 5 seconds.

**Viewer shortcuts** (zoom and pan behave as in FastCuller):

| Key / gesture | Action |
|---------------|--------|
| `←` / `→` | Previous / next series |
| `1`–`5` | Rate the base photo N stars |
| `` ` `` or `0` | Clear the base photo's rating |
| `G` | Back to the gallery |
| `F` | Toggle full screen (also works in the gallery) |
| `I` | Pin / unpin the info panel |
| `S` | Pin / unpin the filmstrip |
| `Space` | Fit the image to the window (reset zoom) |
| `Esc` | Reset zoom if zoomed in, otherwise back to the gallery |
| Pinch (or `Cmd/Ctrl` + scroll) | Zoom in/out, centred on the cursor |
| Two-finger swipe, or drag | Pan while zoomed in |
| Double-click | Zoom in to that point, or reset if already zoomed |

The viewer starts with just the image, full window. Two docks are tucked away and marked by small tabs on the screen edges:
- **Info panel** (right edge): title, Exit button, stars and overlays.
- **Filmstrip** (bottom edge): one thumbnail per series with its star rating; click one to jump to it. It stays centred on the current series.

Move the mouse to an edge to peek a dock; it hides again when the mouse leaves it. Click its tab (or press `I` / `S`) to pin it open. Pins carry over as you move between series and reset when you go back to the gallery. On narrow screens (phones) both stay docked below the image.

Zoom resets when the viewer is opened from the gallery, but carries over when moving between series or when a new shot arrives, so you can check focus in the same crop.

## Testing
```bash
rye run test
```
