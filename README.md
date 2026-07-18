# WifiTether

A local-only utility for reviewing shots in real time while tethering. As the camera writes CR3 (or JPEG) files to a folder, the viewer automatically detects flash vs. no-flash shots, groups them into series, and composites them with screen blending.

**Requirements:**
- Python 3.12+
- [rye](https://rye.astral.sh/)

**Setup:**
```bash
rye sync
rye run wifitether
```

Open **http://localhost:5001**, enter the shoot folder path (e.g. `~/Pictures/2026/2026-05-07`), and click **Watch**.

**How it works:**
- Photos taken **with flash** are treated as base photos.
- Photos taken **without flash** are treated as overlays, associated with the most recent base.
- The gallery shows one card per series: the base photo's thumbnail is a screen-blended composite of the base and all its overlays, with a badge showing the overlay count.
- Clicking a card opens the multiple exposure viewer for that series, where individual overlays can be toggled on and off.
- New files are detected automatically — no need to refresh.
- If the viewer is open when a new photo arrives, it automatically jumps to the most recent overlay.

**Flash detection overrides:**
Some flashes (e.g. off-camera/wireless triggers) never get recorded in the camera's own EXIF Flash tag, which can misclassify a base photo as an overlay. Add a `flash_fired` or `flash_not_fired` keyword (e.g. in Lightroom) to override the detected value for a photo — picked up live via a sidecar `.xmp` or embedded XMP, no restart needed.

**Star ratings:**
- The gallery can be filtered to series whose base photo is rated N stars and up, using the star row above the gallery.
- Ratings are read from a Lightroom catalog (`.lrcat`) if you point the app at one, falling back to a sidecar `.xmp` or embedded XMP otherwise.
- Rating changes made after a photo is loaded are picked up automatically — sidecar/embedded XMP edits are detected instantly, and the Lightroom catalog is polled every 5 seconds.

**Testing:**
```bash
rye run test
```
