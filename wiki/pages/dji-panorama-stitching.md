---
title: DJI Panorama Stitching (Hugin)
tags: [dji, panorama, stitching, hugin, derived-assets, geosorter]
created: 2026-06-05
updated: 2026-06-13
sources: [task:l-panorama-stitch-spike, task:m-panorama-stitch, task:m-fix-panorama-projection-autodetect, task:m-cli-restitch-fix-projection, task:m-fix-wide-pano-stitch-and-failure-ux]
---

# DJI Panorama Stitching (Hugin)

A DJI "sphere"/360 panorama is captured as a directory of overlapping tiles —
`DCIM/PANORAMA/<seq>_<counter>/PANO_0001.JPG … PANO_0035.JPG` (35 tiles, each
~4032×3024 / 12 MP, EXIF intact). **DJI ships no stitched composite and no lens
calibration.** B12 files this as a `panorama` capture unit (primary tile +
`panorama_frame` companions, see [Phase 1 Backend](phase1-backend-api.md) and the
companion-grouping docs); B13 adds an optional, lazy, server-side **stitched
hero** built on top of it, with graceful fallback to the tile gallery whenever
stitching is unavailable. The projection is **auto-detected** from the tile geometry
(m-fix-panorama-projection-autodetect): a full 360 sphere produces an equirectangular
hero (the immersive `PanoSphere` viewer); a non-360 (180/wide/vertical) pano produces a
flat hero (a flat zoomable image). See *Projection auto-detection* and
*Output-validity gate* below.

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
autooptimiser -a -m -l -s -o p.pto p.pto            # optimize positions + photometric + FOV
pano_modify  --projection=<inferred> --canvas=4000x2000 --crop=AUTO -o p.pto p.pto
hugin_executor --stitching --prefix=out p.pto       # nona + enblend -> out.tif
```

- **Projection auto-detection (m-fix-panorama-projection-autodetect).** Not every DJI
  panorama is a full 360 sphere — it also shoots 180, wide, and vertical panos. `--canvas`
  default shrank to `4000x2000` (m-frontend-pano-ux). The projection is **no longer
  hard-coded**: `autooptimiser -s` already estimates the panorama's horizontal field of
  view (HFOV) and writes it to the `.pto` `p`-line (`v<HFOV>`); `panorama_stitch` reads
  that HFOV back (`derived._parse_pto_hfov`) and maps it (`derived._choose_projection`) to
  the `pano_modify --projection` code — **HFOV ≥ 270° → equirectangular (code 2)**;
  **≥ 120° → cylindrical (code 1)**; **otherwise → rectilinear (code 0)**. The last two are
  "flat" (non-360) heroes. This fixes low-tile-count / non-360 panoramas (e.g. the 5.38:1
  result that the old equirectangular-only gate rejected). A full 360 sphere is
  ~35 tiles and still resolves to equirectangular (no regression).
- **Wide single-row 360 reclassification (m-fix-wide-pano-stitch-and-failure-ux).** A
  single-row 360 sweep has HFOV ≥ 270° so `_choose_projection` picks equirectangular, but
  `--crop=AUTO` trims it to a wide thin strip (~8:1) — not a 2:1 sphere, so the equirectangular
  gate `[1.3, 3.0]` rejected it. `panorama_stitch` now **reclassifies** a chosen-equirectangular
  output whose rendered aspect falls outside `[1.3, 3.0]` but inside the flat envelope to
  **flat** — one-way (a true sphere renders within `[1.3, 3.0]` and is unaffected), reusing the
  already-rendered output (no second Hugin pass) — so it passes the (widened) flat gate, records
  `stitch_projection='flat'`, and routes to the flat hero instead of failing.
- `--projection=2` is **equirectangular** (the 360×180 sphere flattened to a 2:1
  rectangle); `--crop=AUTO` trims empty margins, so a real 360 output is ~2:1.
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
result (the cv2-style void, or a failed run) is never shown as a hero. The gate is
**projection-aware** (`_stitch_gate(path, kind)`, m-fix-panorama-projection-autodetect):

- long edge within `[2000, 6000]` px (the cap also bounds `enblend` time/memory) — both
  projections,
- aspect — **equirectangular** within `[1.3, 3.0]` (lenient for `--crop=AUTO`); a
  non-360 **flat** hero within the far wider `[0.2, 16.0]` (a 180/wide pano is legitimately
  up to ~6:1, a single-row 360 sweep is a wide thin strip ~8:1 and up, a vertical pano is
  tall, < 1), so it is no longer wrongly rejected as "not equirectangular-like" — the max was
  raised 8.0 → 16.0 (m-fix-wide-pano-stitch-and-failure-ux),
- near-black-pixel fraction `≤ 15 %` (luminance < 8, via the histogram) — both
  projections; the direct guard against the cv2 ~45 %-void failure mode.

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
  failure — the gallery is the expected experience). The map UI surfaces a
  `stitch_status==='failed'` panorama with a persistent `badge--stitch-failed` in the
  file-list panel (GeoJSON-driven, survives reload), and the "Stitch all panoramas" run
  reports a failed count (`runStitchAll`'s `failedIds`, which excludes the non-failure
  `unavailable` case) — so a stuck panorama is identifiable, not just a number
  (m-fix-wide-pano-stitch-and-failure-ux).
- **`stitch_projection` provenance + viewer choice** (schema v4,
  m-fix-panorama-projection-autodetect). A second nullable `files.stitch_projection`
  column (`NULL` | `equirectangular` | `flat`, panorama rows only) records the detected
  projection alongside the `ok` status, so the map UI picks the hero viewer straight off
  the `/api/library` GeoJSON: **equirectangular → the 360 `PanoSphere` sphere viewer**;
  **flat → a flat zoomable image (`FlatHero`)**. A `NULL` (legacy hero stitched before
  this feature — all of which were 360, since the old gate rejected everything else)
  defaults to `PanoSphere`. The `/api/library` ETag folds a `stitch_projection` code sum
  so a projection change can't 304 stale. On a freshness cache hit (no fresh HFOV) the
  projection is **backfilled from the cached hero's aspect only when `NULL`**
  (`classify_stitched_projection`) — recovering a cache that outlived its index-DB row,
  never overwriting the authoritative cold-run HFOV-derived value.
- **Traversal-proof serving.** `GET /api/stitch/{file_id}` is **file-id-keyed**: it
  derives the `.jpg` cache path server-side from the stored primary `dest_path` and
  never accepts a client relpath, so the `.pto`/`out.tif` intermediates (which live
  only in an auto-cleaned temp dir) are structurally unservable.

## Retroactive re-stitch (m-cli-restitch-fix-projection)

Heroes stitched **before** projection auto-detection were forced into the hard-coded
equirectangular canvas, so a non-360 (180/wide/vertical) pano had the **wrong geometry
baked into the cached JPEG** — re-recording `stitch_projection` alone cannot fix it; the
Hugin pipeline must be **re-run**. The `geosorter restitch` CLI verb is that one-shot
migration (`src/geosorter/restitch.py`, mirrors `rescan.py`):

- **Selection.** Default = `capture_kind='panorama' AND stitch_status='ok' AND
  stitch_projection IS NULL` — a **precise "predate the fix" marker**, since
  `stitch_projection` is written *only* by the auto-detect code. `--all` drops the NULL
  clause (re-stitches every `ok` panorama; minutes each).
- **Force cold re-stitch.** It re-runs `derived.panorama_stitch(..., force=True)`, a new
  keyword that skips **only** the freshness cache early-return so the pipeline runs cold
  and returns the freshly auto-detected projection. **Failure-safe:** `_stitch_gate`/Hugin
  steps raise *before* the final `_atomic_write`, so a failed re-stitch leaves the existing
  (wrong-but-present) hero untouched; a success atomically replaces it. The new projection
  is then written to `files.stitch_projection`.
- **Index-DB-only + per-row resilience.** Like `rescan`, it writes only the index DB
  (the two stitch columns) and never moves/deletes a media file; the cached hero is the
  sole on-disk artifact replaced (regenerable). A `StitchFailed` or a missing-on-disk row
  (`OSError`) is reported per-row and skipped — it never aborts the batch. A failed
  re-stitch is **not** flipped to `stitch_status='failed'` (least-destructive, unlike
  `jobs._run_stitch`). Hugin absent → reported `unavailable`, nothing written.
- **Scope.** CLI-only (`restitch [--all] [--dry-run] [--yes]`) — no API route, no job
  pool, no frontend change. The map UI already routes `flat → FlatHero` and
  `equirectangular → PanoSphere` off the `/api/library` GeoJSON, so a corrected projection
  shows on the next library load.

## Install

Hugin (the whole CLI suite: `pto_gen`, `cpfind` incl. `celeste`, `cpclean`,
`autooptimiser`, `pano_modify`, `nona`, `enblend`, `hugin_executor`) installs via
winget `Hugin.Hugin` (Windows), apt `hugin-tools` (Debian/Ubuntu), or brew `hugin`
(macOS). On Windows it lands at `C:\Program Files\Hugin\bin`; point the optional
`hugin_bin_dir` config key there if it is not on `PATH`.
