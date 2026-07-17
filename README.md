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
- The gallery shows all photos in shooting order: base photos as-is, overlay photos composited with their base.
- Clicking any card opens the multiple exposure viewer for that series; clicking an overlay card pre-enables that overlay.
- New files are detected automatically — no need to refresh.
- If the viewer is open when a new photo arrives, it automatically jumps to the most recent overlay.

**Testing:**
```bash
rye run test
```
