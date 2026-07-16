---
title: Backend, API, Web UI & Derived Assets
tags: [api, fastapi, geojson, hevc, architecture, frontend, auth, jobs, geosorter]
created: 2026-06-01
updated: 2026-07-16
sources: [src/geosorter/api.py, src/geosorter/jobs.py, src/geosorter/derived.py, frontend/src, task:m-library-feed-scale, task:m-derived-at-scale]
---

# Backend, API, Web UI & Derived Assets

The FastAPI backend (`geosorter.api`) exposes the organized library over HTTP for
the React map browser. It is built by `create_app(cfg, *, spa_dir=None)` and
launched by `geosorter serve`. This page describes the current contract used by
the frontend and other clients; historical B6/B7/B8 labels are no longer a
statement about product completeness.

## Security posture

- **Binds `127.0.0.1` by default.** `serve --host <addr>` explicitly opts into
  network exposure. A non-loopback bind warns because library reads include media
  and GPS coordinates.
- **Optional admin authentication.** Without `admin_password_hash`, the app and API
  are open. With a password configured by `set-admin-password`, read routes remain
  public but management controls are hidden until login and mutating routes require
  an in-memory bearer token. Tokens reset on restart. Five failed passwords lock a
  client out for 30 seconds; `/api/login` returns 429 with `Retry-After` while
  throttled.
- **The admin password does not protect read access.** A non-loopback bind still
  exposes the map, GPS locations, originals, and derived media to that network.
- **Traversal and cache guards.** Relpath-based media routes resolve under
  `library_root`, reject escapes with HTTP 403, and also reject any
  `.geosorter-cache` path component (case-insensitive). File-id-keyed panorama
  routes derive cache paths server-side. Missing files return 404.

## Endpoints

- `GET /api/library` → a whole-library GeoJSON `FeatureCollection` containing one
  point per organized, geolocated capture. Properties are `id`, `filename`,
  `place_string`, `local_date`, `capture_ts_local`, `media_type`, `codec`,
  `gps_source`, `capture_kind`, `frame_count`, `star_rating`, `stitch_status`,
  `stitch_projection`, `has_track`, and library-relative `path`. Coordinates are
  `[lon, lat]`. The route supports conditional GET and route-scoped gzip.
- `GET /api/inbox` → `{files, captures}`. `GET /api/inbox/list` → capture groups
  for the selective Process Inbox panel. The scan excludes `_duplicates`.
- `GET /api/quarantine` lists no-GPS captures. `GET /api/place-search?q=...`
  performs offline GeoNames forward search for assignment.
- `GET /api/frames/{file_id}` lists hyperlapse/panorama source frames.
  `GET /api/track/{file_id}` returns downsampled route points and timestamped
  samples from a video's SRT sidecar.
- `GET /api/media/{relpath}` serves originals with HTTP Range support.
  `GET /api/thumb`, `/api/preview`, and `/api/poster` serve lazy cached JPEGs.
  `GET /api/video` serves H.264 directly or a cached H.264 proxy for HEVC.
- `GET /api/collage/{file_id}` serves the instant panorama tile collage.
  `GET /api/stitch/{file_id}` serves a generated Hugin hero.
- `GET /api/auth`, `POST /api/login`, and `POST /api/logout` manage the optional
  admin session.
- Organize, undo, re-tag, assign-location, rescan, and stitch each have start and
  status routes. Organize and undo also have cancel routes. Start/cancel mutations
  require admin authentication when configured; status routes stay readable.
- Static SPA: when the built frontend exists, `create_app` mounts it at `/` after
  the API routes, so browser and API share an origin and need no CORS setup.

The stored `files.dest_path` is an absolute Windows path with a `\\?\` long-path
prefix; `api._strip`/`api._relpath` convert it to the library-relative POSIX
`path` used in URLs, and `_safe_path` reverses that for serving.

## HEVC strategy

DJI libraries commonly contain both H.264 and HEVC/H.265. Chrome and Firefox on
Windows do not reliably play HEVC in a `<video>` element.

Chosen strategy: **cached H.264 proxy transcode.** `/api/video` looks up the
file's codec in the `files` table; H.264 streams the original, while HEVC triggers
ffmpeg and caches the result in the proxy tier. `proxy_hwaccel` selects `auto`,
`nvenc`, or `none`: auto uses NVIDIA NVENC when detected and retries once with
libx264 if NVENC fails; strict NVENC surfaces a failure. `warm-proxies` can create
these proxies before first playback and `proxy-bench` compares both encoders.

## Derived assets — lazy, cached, atomic

`geosorter.derived` generates thumbnails, 1920px-long-edge previews, video poster
frames, panorama collages, and HEVC proxies. Cheap image assets live under the
local `cache_dir`; proxies and stitches live under `proxy_cache_dir` (which
defaults to the library root when unset). Relative source keys are mirrored under
each cache kind.

- **Freshness** requires cache mtime ≥ source mtime and, when a `.src` sidecar is
  present, the same source byte size. Legacy sidecar-less assets retain the
  mtime-only fallback.
- **Invalidation** removes every cached kind for old/new relpaths when organize,
  re-tag, or no-GPS assignment moves or replaces content. This prevents an older
  source mtime from making a stale poster look fresh.
- **Atomicity**: every asset is produced via `_atomic_write` — a unique
  `mkstemp` temp in the cache dir, then `os.replace` into place — so a concurrent
  first-request never observes a half-written file. This mirrors the
  partial→replace discipline of the [crash-safe move engine](crash-safe-move-engine.md).
- ffmpeg/ffprobe are invoked list-form (never `shell=True`), matching `metadata.py`.

### At scale — generation cap, warm pass, eviction (m-derived-at-scale)

The current controls keep on-demand generation from falling over on a 5–20k-file library
(the cache is tiered by `m-cache-tiering-safety`: thumbs/posters/previews on a local
SSD `cache_dir`, proxies/stitch on `proxy_cache_dir`):

- **Concurrency caps.** A process-wide `DERIVED_MAX_CONCURRENCY = 4` semaphore
  bounds general image generation. HEVC transcodes use a separate
  `PROXY_MAX_CONCURRENCY = 2` semaphore, so long ffmpeg runs cannot occupy every
  permit and starve thumbnails/previews/posters. A
  fresh **cache hit returns before acquiring**, so hits stay lock-free; `_generate`
  re-checks `_is_fresh` *under* the permit to drop a lost race. The cap is
  process-wide because `serve` is single-process uvicorn (a brainstorm decision — no
  multi-worker, which would fragment the in-memory `JobManager`).
- **Post-organize warm pass** (`geosorter.warm.warm_library`). After an organize with
  `organized > 0`, `JobManager._run` auto-enqueues a warm job on a **dedicated
  `_warm_pool`** (independent of the destructive and stitch pools) that pre-generates
  thumbnails (photos) + posters (videos) for that batch on the local tier, skipping
  fresh/missing sources (resumable). Generation goes through the shared cap, so the
  warm pass **yields to foreground** requests. It is silent (no API route/UI) and
  kept in its own module so `derived.py` stays DB-free.
- **Local-tier eviction** (`derived.evict_local_cache`). The warm job ends by
  atime-sweeping the **local** tier (`thumbs`/`previews`/`posters`/`collage`) down to
  `cache_max_gb`, deleting least-recently-accessed first; a locked file
  (`PermissionError`) or one that raced away (`FileNotFoundError`) is tolerated, never
  fatal. When `proxy_cache_max_gb` is configured, proxies get their own LRU cap;
  stitch heroes are never evicted. `clear-derived-cache` clears only the cheap
  local kinds.
- **Permanent corruption handling.** High-confidence ffmpeg/Pillow decode failures
  cache and serve a neutral placeholder for image derivatives. Unrenderable video
  returns 422, while transient generation failures return 503 and remain retryable.

## Background jobs (`geosorter.jobs`)

`JobManager` runs destructive/index-mutating work off the request cycle on a
`ThreadPoolExecutor(max_workers=1)`. Organize, undo, re-tag, no-GPS assignment,
and rescan therefore cannot execute concurrently. `submit()` returns a uuid4
job id; per-job state is polled via `status()` (a `dataclasses.replace()` snapshot
taken under lock). Cancellation is a per-job `threading.Event` wired into
`run_organize`'s `cancel` predicate, which is polled **between capture groups**
only — never mid-group — so the group-atomic copy→verify→delete invariant of the
[move engine](crash-safe-move-engine.md) is preserved; a cancelled run leaves
unprocessed captures in the inbox. This is deliberately **not** FastAPI
`BackgroundTasks` (those have no id, status, or cancellation). API-triggered jobs
run with `assume_yes=True` (the interactive first-run gate cannot prompt over HTTP).

Undo cancellation is polled between files; organize cancellation is polled
between capture groups. Re-tag, assignment, and rescan have no cancel route. The
manager permits organize to queue behind organize for the Process Inbox flow, but
rejects dangerous cross-kind submissions with `WorkerBusy`/HTTP 409.

Panorama stitches use a dedicated `max_workers=1` pool, independent of the
destructive worker. Post-organize warming uses a third single-worker pool.

## Scalable library feed (conditional GET + scoped gzip, m-library-feed-scale)

The feed is **loaded whole** (no bbox/clustering on the server), so at 5–20k
features it can be a multi-megabyte JSON blob revalidated after management
operations. Two HTTP-level wins keep that cheap without
server-side pagination:

- **Conditional GET.** `GET /api/library` runs a cheap version probe first and
  short-circuits a matching `If-None-Match` to **`304` before building the feature
  list**. The weak ETag includes a payload-schema token plus
  `MAX(id)`, `COUNT(*)`, latitude/longitude sums, stitch status, and stitch projection
  over the organized+geolocated rows. `MAX(id)`+`COUNT(*)` catch add/prune, but the
  **in-place** UPDATEs (`retag.py` moves `lat`/`lon`; `jobs._mark_stitch_status`
  flips stitch state) leave both unchanged — so the key also folds in
  microdegree lat/lon sums and stitch codes. The schema token was bumped when
  `has_track` was added. **Lesson:** an ETag keyed
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
[organize pipeline](crash-safe-move-engine.md); location, local time, media,
capture-kind, rating, and stitch properties come from indexed columns (see
[capture time & geocoding](capture-time-and-geocoding.md) for how those values are
derived). `has_track` is calculated from indexed SRT companions. The two-database
split is unchanged: most API reads use the index DB, while `/api/place-search`
queries the GeoNames DB.

## Frontend SPA (`frontend/`)

A Vite + React 19 + TypeScript single-page app (`react-map-gl` MapLibre adapter,
`maplibre-gl`, `supercluster`) consumes the contract above. It builds to
`src/geosorter/webui` — the dir `create_app` mounts — so it is served same-origin
by `geosorter serve` (no CORS); the build output is gitignored and produced on
demand (`npm --prefix frontend run build`). Dev runs the Vite server with `/api`
proxied to `127.0.0.1:8000`.

Key choices: **OpenFreeMap** hosted vector tiles (no key; the main online
dependency — photos/GPS stay local), and
**client-side clustering** with the explicit `supercluster` package in a pure,
unit-tested module (the `/api/library` feed is loaded whole, so the map — not the
server — does the clustering; `getClusters(bbox, zoom)` returns only the visible
clusters/points, keeping DOM marker count low). Pure logic (URL builders,
clustering, the organize-job polling state machine, marker→files selection) lives
in side-effect-free modules with Vitest coverage; the React components and the
visual end-to-end flow are browser-tested during UI work. The lightbox loads photos
from `/api/preview` (1080p) and plays videos from `/api/video` (H.264 proxy for
HEVC).

The current UI adds a virtualized, viewport-scoped file rail; day/month/year
grouping; newest/oldest sort; photo/video/panorama/hyperlapse filters; a resizable
desktop rail and snap-height mobile bottom sheet; selective Process Inbox;
Locations, No-GPS, and Unstitched Panorama panels; administrator-only re-tag,
assign, undo, rescan, and stitch controls; and retry/error states for failed
library or job loads.

Videos with `has_track` can draw their SRT route on the map. The viewer becomes a
draggable picture-in-map player, timestamped track samples move a drone marker in
sync with playback, and an optional follow mode keeps the map centered on it.

**View toggles.** A pure `basemaps.ts` module holds the basemap styles and the
heatmap spec: `VECTOR_STYLE` (OpenFreeMap), `SATELLITE_STYLE` (an Esri World Imagery
**raster** style object with attribution), `HEATMAP_LAYER` (a native MapLibre
`type:'heatmap'` layer), and `heatmapData(features)` (a GeoJSON `FeatureCollection`).
An on-map `.map-controls` panel switches the **Satellite** basemap (`mapStyle`
swap) and a **Heatmap** density layer (`<Source>`/`<Layer>`); turning the heatmap on
hides the cluster markers + legend for a clean density view. `basemaps.ts` is
side-effect-free with Vitest coverage; the toggle wiring is component glue.
