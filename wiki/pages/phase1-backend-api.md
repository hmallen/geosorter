---
title: Phase 1 Backend — HTTP API Contract & Derived Assets
tags: [api, fastapi, geojson, hevc, architecture, phase-1, undo, phase-2]
created: 2026-06-01
updated: 2026-06-10
sources: [dji-media-organizer.md, h-api-backend.md, task:h-undo-batch, task:h-neighbor-gps-inference, task:h-retag-location, task:m-basemap-heatmap-toggles, task:m-library-feed-scale]
---

# Phase 1 Backend — HTTP API Contract & Derived Assets

The B6 FastAPI backend (`geosorter.api`) exposes the organized library over HTTP
for the B7 map-viewer frontend. It is built by `create_app(cfg, *, spa_dir=None)`
and launched by the `geosorter serve` CLI verb. This page is the **contract** the
frontend (and any other client) builds against.

## Security posture

- **Binds `127.0.0.1` by default.** The library's GeoJSON embeds *home GPS
  coordinates* and there is **no authentication**. `serve --host <addr>` is an
  explicit opt-in; any non-loopback host prints a no-auth/home-GPS exposure
  warning (`cli._resolve_host`). Loopback values (`127.0.0.1`, `localhost`, `::1`)
  do not warn.
- **Path-traversal guard.** `/api/media`, `/api/thumb`, `/api/poster`, `/api/video`
  resolve the request path under `library_root` and reject anything that escapes:
  `(library_root / relpath).resolve()` must be `is_relative_to(library_root)`,
  else HTTP 403. A missing file is 404.

## Endpoints

- `GET /api/library` → GeoJSON `FeatureCollection`, **loaded once** (no
  bbox/viewport endpoint — clustering is the client's job, via supercluster in
  B7). One `Point` feature per **organized, geolocated** file; quarantined/no-GPS
  files are excluded (they have no coordinate to place). Feature `properties`:
  `id`, `filename`, `place_string`, `local_date`, `media_type`, `codec`,
  `gps_source` (`exif`|`srt`|`srt_partial`|`inferred`|`manual`|`none`|null — B8; the
  map UI styles `inferred` (amber/dashed) and `manual` (green) markers distinctly),
  and `path` (library-relative POSIX path used to build media URLs). Coordinates are
  `[lon, lat]` (GeoJSON order). Supports **conditional GET**: emits a weak `ETag` +
  `Last-Modified` and answers a matching `If-None-Match` with `304` (see *Scalable
  library feed* below). The JSON body is **gzipped in-route** when the client accepts it.
- `GET /api/inbox` → `{files, captures}` — how much is waiting for the next
  `organize` run (B8). `files` = recursive file count under `inbox_path` (what
  `organize` scans); `captures` = DJI capture-group count (`group_companions`, what
  `organize` processes). Scan-on-request (no `watchdog` observer); the frontend polls
  it for a toolbar badge. Returns `{0, 0}` when the inbox is unset/missing/empty.
- `POST /api/organize` → `{job_id}`; `GET /api/organize/status/{id}` → the job
  snapshot; `POST /api/organize/cancel/{id}` → sets the cancel flag. See
  *Background jobs* below.
- `POST /api/undo` → `{job_id}`; `GET /api/undo/status/{id}` → the undo-job
  snapshot; `POST /api/undo/cancel/{id}` → sets the cancel flag (B8). Reverses the
  most recent `organize` batch back to the inbox; see the
  [undo section](crash-safe-move-engine.md) for the reverse-move model. The two
  cancel routes are partitioned by job kind (a route 404s on the other kind's id).
- `POST /api/retag` (body `{file_id, lat, lon}`, lat/lon WGS84-bounded) → `{job_id}`;
  `GET /api/retag/status/{id}` → the re-tag-job snapshot (B8). Re-files an organized
  capture to a map-clicked location; see the
  [re-tag section](crash-safe-move-engine.md) for the move model. **No cancel route**
  — a re-tag is a fast atomic single-capture op, unlike the long-running
  organize/undo. Shares the single-worker pool, so organize, undo, and re-tag are
  mutually exclusive.
- `GET /api/media/{relpath}` → the original file via range-capable
  `starlette.responses.FileResponse` (HTTP 206 for `Range` requests → video seek,
  large-photo download). **Not** a bare `StreamingResponse`.
- `GET /api/thumb/{relpath}` (512px grid thumbnail) / `GET /api/preview/{relpath}`
  (1080p / 1920px long-edge, lightbox photo, B7) / `GET /api/poster/{relpath}`
  (video poster frame) → lazily generated, cached derived JPEGs.
- `GET /api/video/{relpath}` → a **browser-playable** video (range-capable). H.264
  originals are served directly; HEVC is served as a cached H.264 proxy. The
  frontend points every `<video>` here regardless of source codec.
- Static SPA: when a build directory exists, `create_app` mounts it at `/` (after
  the `/api` routes) so the frontend is served same-origin (no CORS). B6 ships the
  mount point; B7 fills the directory.

The stored `files.dest_path` is an absolute Windows path with a `\\?\` long-path
prefix; `api._strip`/`api._relpath` convert it to the library-relative POSIX
`path` used in URLs, and `_safe_path` reverses that for serving.

## HEVC strategy (the gating decision)

DJI codec mix, probed from real footage: the **current drone (Mini 4 Pro) records
HEVC/H.265**, an older drone records H.264 — ~60% HEVC and rising. Browsers
(Chrome/Firefox on Windows) do **not** reliably play HEVC in a `<video>` element.

Chosen strategy: **on-demand H.264 proxy transcode.** `/api/video` looks up the
file's codec in the `files` table; H.264 streams the original, HEVC triggers an
ffmpeg `libx264` transcode cached under `.geosorter-cache/proxies/`. Rejected
alternatives: direct-stream (majority of videos silently fail) and
poster-plus-"coming soon" (majority unplayable in Phase 1).

## Derived assets — lazy, cached, atomic

`geosorter.derived` generates thumbnails (512px JPEG, Pillow
`ImageOps.exif_transpose`), video poster frames (ffmpeg first frame), and HEVC
proxies **on first request**, caching them under
`library_root/.geosorter-cache/{thumbs,posters,proxies}/` (the source's
library-relative path is mirrored under each kind dir). This keeps the crash-safe
Phase 0 `organize` pipeline free of any Pillow/ffmpeg dependency.

- **Freshness** is mtime-based (`_is_fresh` = cache mtime ≥ source mtime); no
  hashing. A re-organized source regenerates.
- **Atomicity**: every asset is produced via `_atomic_write` — a unique
  `mkstemp` temp in the cache dir, then `os.replace` into place — so a concurrent
  first-request never observes a half-written file. This mirrors the
  partial→replace discipline of the [crash-safe move engine](crash-safe-move-engine.md).
- ffmpeg/ffprobe are invoked list-form (never `shell=True`), matching `metadata.py`.

## Background jobs (`geosorter.jobs`)

`JobManager` runs `organize` off the request cycle on a `ThreadPoolExecutor(
max_workers=1)` — only one destructive pass at a time. `submit()` returns a uuid4
job id; per-job state is polled via `status()` (a `dataclasses.replace()` snapshot
taken under lock). Cancellation is a per-job `threading.Event` wired into
`run_organize`'s `cancel` predicate, which is polled **between capture groups**
only — never mid-group — so the group-atomic copy→verify→delete invariant of the
[move engine](crash-safe-move-engine.md) is preserved; a cancelled run leaves
unprocessed captures in the inbox. This is deliberately **not** FastAPI
`BackgroundTasks` (those have no id, status, or cancellation). API-triggered jobs
run with `assume_yes=True` (the interactive first-run gate cannot prompt over HTTP).

The B8 **undo** job (`submit_undo`/`undo_status`/`_run_undo`, `UndoJobState`) is a
second job kind on the **same** `JobManager` — it shares that one `max_workers=1`
executor and the cancel-event table, so an organize and an undo can never run
concurrently against the same library/inbox. Its `cancel` is polled **between
files**.

The B8 **re-tag** job (`submit_retag`/`retag_status`/`_run_retag`, `RetagJobState`)
is the third kind on the same executor — so organize, undo, and re-tag are mutually
exclusive (no concurrent index-DB writers). It has **no cancel** (a single-capture
atomic move; nothing to interrupt). `retag_fn=retag.retag_file` is injectable for
tests. See the [re-tag section](crash-safe-move-engine.md) for the move model.

## Scalable library feed (conditional GET + scoped gzip, m-library-feed-scale)

The feed is **loaded whole** (no bbox/clustering on the server), so at 5–20k
features it is an 8–12 MB JSON blob re-fetched on every reload (after
organize/undo/retag/rescan). Two HTTP-level wins keep that cheap without
server-side pagination:

- **Conditional GET.** `GET /api/library` runs a cheap version probe first and
  short-circuits a matching `If-None-Match` to **`304` before building the feature
  list**. The weak `ETag` is `W/"lib-<MAX(id)>-<COUNT(*)>-<latsum>-<lonsum>-<stitchsum>"`
  over the organized+geolocated rows. `MAX(id)`+`COUNT(*)` catch add/prune, but the
  **in-place** UPDATEs (`retag.py` moves `lat`/`lon`; `jobs._mark_stitch_status`
  flips `stitch_status`) leave both unchanged — so the key also folds in
  microdegree lat/lon sums and a `stitch_status` code sum. **Lesson:** an ETag keyed
  only on row identity/count silently 304s in-place edits, leaving the map showing a
  stale marker after a retag; key it on the mutated content too. `idx_files_status_latlon
  (status, lat, lon)` (in `_INDEX_SCHEMA`) serves the probe's row selection.
- **Scoped gzip.** The JSON body compresses **in-route** (≈1–2 MB at 20k) only when
  `Accept-Encoding: gzip`. This is deliberately **not** a global Starlette
  `GZipMiddleware`: that wraps every response, and on the range-capable video
  `FileResponse` (served via streamed `http.response.body` under uvicorn, which lacks
  the `pathsend` extension) it strips `Content-Length` and adds `Content-Encoding`,
  **breaking HTTP Range seeking** — plus it wastefully re-compresses already-compressed
  JPEG/MP4. Keeping gzip on the one JSON route protects the media/video routes.

Client (`useLibrary.ts`/`api.ts`): `fetchLibrary` threads the stored ETag as
`If-None-Match` and returns `{fc, etag, notModified}`; on a `304` the hook keeps the
prior `features` visible (stale-while-revalidate — no blank map on reload).

A related cheap win: `_lookup_codec` (for `/api/video`) resolves the codec by an
**indexed equality** on the UNIQUE `files.dest_path` (reconstructing the stored
plain + `\\?\`-prefixed form from the URL relpath) instead of scanning every video
row and recomputing `_relpath`.

## Source of the GeoJSON

The feed reads the index DB `files` table populated by the
[organize pipeline](crash-safe-move-engine.md); `lat`/`lon`/`place_string`/
`local_date`/`codec` come straight from columns written at organize time (see
[capture time & geocoding](capture-time-and-geocoding.md) for how those values are
derived). The two-database split (decision D24) is unchanged — the API only reads
the index DB plus, for the codec lookup, the same table.

## Frontend SPA (`frontend/`, B7)

A Vite + React + TypeScript single-page app (`react-map-gl` v8 MapLibre adapter,
`maplibre-gl`, `supercluster`) consumes the contract above. It builds to
`src/geosorter/webui` — the dir `create_app` mounts — so it is served same-origin
by `geosorter serve` (no CORS); the build output is gitignored and produced on
demand (`npm --prefix frontend run build`). Dev runs the Vite server with `/api`
proxied to `127.0.0.1:8000`.

Key choices: **OpenFreeMap** hosted vector tiles (no key; the only online
dependency — photos/GPS stay local; offline pmtiles is a Phase 2 option), and
**client-side clustering** with the explicit `supercluster` package in a pure,
unit-tested module (the `/api/library` feed is loaded whole, so the map — not the
server — does the clustering; `getClusters(bbox, zoom)` returns only the visible
clusters/points, keeping DOM marker count low). Pure logic (URL builders,
clustering, the organize-job polling state machine, marker→files selection) lives
in side-effect-free modules with Vitest coverage; the React components and the
visual end-to-end flow are verified by a manual smoke. The lightbox loads photos
from `/api/preview` (1080p) and plays videos from `/api/video` (H.264 proxy for
HEVC); "Process Inbox" drives `POST /api/organize` + status polling.

**View toggles (B8).** A pure `basemaps.ts` module holds the basemap styles and the
heatmap spec: `VECTOR_STYLE` (OpenFreeMap), `SATELLITE_STYLE` (an Esri World Imagery
**raster** style object with attribution), `HEATMAP_LAYER` (a native MapLibre
`type:'heatmap'` layer), and `heatmapData(features)` (a GeoJSON `FeatureCollection`).
An on-map `.map-controls` panel switches the **Satellite** basemap (`mapStyle`
swap) and a **Heatmap** density layer (`<Source>`/`<Layer>`); turning the heatmap on
hides the cluster markers + legend for a clean density view. `basemaps.ts` is
side-effect-free with Vitest coverage; the toggle wiring is component glue.
