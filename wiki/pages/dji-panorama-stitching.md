---
title: DJI Panorama Stitching (Hugin)
tags: [dji, panorama, stitching, hugin, derived-assets, geosorter]
created: 2026-06-05
updated: 2026-06-05
sources: [task:l-panorama-stitch-spike, task:m-panorama-stitch]
---

# DJI Panorama Stitching (Hugin)

A DJI "sphere"/360 panorama is captured as a directory of overlapping tiles —
`DCIM/PANORAMA/<seq>_<counter>/PANO_0001.JPG … PANO_0035.JPG` (35 tiles, each
~4032×3024 / 12 MP, EXIF intact). **DJI ships no stitched composite and no lens
calibration.** B12 files this as a `panorama` capture unit (primary tile +
`panorama_frame` companions, see [Phase 1 Backend](phase1-backend-api.md) and the
companion-grouping docs); B13 adds an optional, lazy, server-side **stitched
360 equirectangular hero** built on top of it, with graceful fallback to the tile
gallery whenever stitching is unavailable.

## Why Hugin, not OpenCV (the spike's central finding)

The B13 spike evaluated two stitchers on the **same** real 35-tile pano:

- **OpenCV `cv2.Stitcher` (PANORAMA mode) — NO-GO.** Off-the-shelf and fragile: it
  **crashed the process two ways** on real tiles — a FLANN `knn` assertion on a
  near-featureless **sky** tile (one frame yielded **0 ORB keypoints**), and an
  OpenCL `CL_OUT_OF_RESOURCES` abort in `buildWarpSphericalMaps`. A success required
  pre-filtering the featureless frames AND disabling OpenCL, and even then produced a
  44 MP result that was **~45 % black void** (the excluded zenith/nadir caps) with
  heavy curvature — *worse* than the gallery.
- **Hugin CLI pipeline — GO.** Hugin's calibrated pipeline ran on the **full 35-tile
  set** with every stage `rc=0`, no manual pre-filtering, no crashes, and produced a
  **clean, 100 %-covered, servable 16 MP equirectangular**.

The decisive difference is **`celeste`** — Hugin's sky-detection masks the
featureless sky region that starves feature matching and crashes cv2. Hugin also
optimizes a lens model and blends with `enblend` (multiband), which is why its
output is seam-free where cv2's was a warped partial.

## The pipeline (measured, full-res, all stages `rc=0`)

```
pto_gen      -o p.pto <all tiles>                  # project from the tile set
cpfind       --multirow --celeste -o p.pto p.pto   # control points; celeste masks sky
cpclean      -o p.pto p.pto                         # prune bad control points
autooptimiser -a -m -l -s -o p.pto p.pto            # optimize positions + photometric
pano_modify  --projection=2 --canvas=6000x3000 --crop=AUTO -o p.pto p.pto
hugin_executor --stitching --prefix=out p.pto       # nona + enblend -> out.tif
```

- `--projection=2` is **equirectangular** (the 360×180 sphere flattened to a 2:1
  rectangle). `--canvas=6000x3000` caps the sphere; `--crop=AUTO` trims empty margins,
  so the real output is ~**6000×2683** (16 MP, aspect ≈ 2.24:1).
- **Cost:** ~**7 min** wall-clock (`cpfind` ~188 s + `autooptimiser` ~81 s +
  `enblend` ~136 s), peak ~**434 MiB** RSS (`enblend` streams/tiles, so it is *more*
  memory-frugal than cv2's ~2 GB). This cost is why stitching is **lazy, cached, and
  background-only** — never on the ingest/move path.

Hugin is consumed purely as an **external CLI tool** (no `hsi` SWIG binding, no
linking), exactly like the existing `ffmpeg`/`exiftool` integrations — so it adds no
Python dependency and no new licensing surface (GPL out-of-process is fine). It is
**runtime-detected and optional**: absent → no stitch is attempted and the UI keeps
the tile gallery.

## Output-validity gate

A stitch is only cached/served after passing a cheap Pillow gate, so a degenerate
result (the cv2-style void, or a failed run) is never shown as a hero:

- long edge within `[2000, 6000]` px (the cap also bounds `enblend` time/memory),
- aspect within `[1.3, 3.0]` (equirectangular-like, lenient for `--crop=AUTO`),
- near-black-pixel fraction `≤ 15 %` (luminance < 8, via the histogram) — this is the
  direct guard against the cv2 ~45 %-void failure mode.

A failed pipeline step, a timeout, or a gate rejection all surface as a single
`failed` outcome → the client falls back to the gallery.

## Architecture decisions (geosorter)

- **Lazy, user-triggered, cached.** ~7 min is too long for ingest or for an
  auto-on-open. The stitch is started by an explicit **"Generate stitched panorama"**
  lightbox button, generated once, then mtime-cached under
  `.geosorter-cache/stitch/<rel>.jpg` (keyed on the primary tile, like every derived
  asset). Re-tag/re-organize moves the primary → new cache key → regenerates.
- **Dedicated read-only worker pool.** A stitch reads already-organized tiles and
  writes only the cache — strictly **off the crash-safe move path**. It runs on its
  own `max_workers=1` pool, *separate* from the destructive organize/undo/retag
  worker, so a 7-min stitch never blocks (or waits behind) a destructive job, yet
  stitches still serialize to one at a time (bounding CPU/RAM).
- **`stitch_status` provenance.** A nullable `files.stitch_status` column
  (`NULL`=not attempted | `pending` | `ok` | `failed`, schema v3, panorama rows only)
  lets the map UI know whether a hero exists without probing the cache. Hugin-absent
  resolves to a job status of `unavailable` and leaves the column `NULL` (not a
  failure — the gallery is the expected experience).
- **Traversal-proof serving.** `GET /api/stitch/{file_id}` is **file-id-keyed**: it
  derives the `.jpg` cache path server-side from the stored primary `dest_path` and
  never accepts a client relpath, so the `.pto`/`out.tif` intermediates (which live
  only in an auto-cleaned temp dir) are structurally unservable.

## Install

Hugin (the whole CLI suite: `pto_gen`, `cpfind` incl. `celeste`, `cpclean`,
`autooptimiser`, `pano_modify`, `nona`, `enblend`, `hugin_executor`) installs via
winget `Hugin.Hugin` (Windows), apt `hugin-tools` (Debian/Ubuntu), or brew `hugin`
(macOS). On Windows it lands at `C:\Program Files\Hugin\bin`; point the optional
`hugin_bin_dir` config key there if it is not on `PATH`.
