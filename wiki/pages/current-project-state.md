---
title: Current Project State
tags: [current-state, architecture, cli, api, frontend, operations, geosorter]
created: 2026-07-16
updated: 2026-08-23
sources: [README.md, pyproject.toml, geosorter.example.toml, src/geosorter, frontend/src]
---

# Current Project State

As of **2026-07-16**, geosorter is a working local-first DJI media organizer and
browser, not a roadmap prototype. The Python backend, crash-safe file pipeline,
SQLite indexes, offline geocoding, React/MapLibre interface, maintenance tools,
and optional Hugin panorama workflow are all implemented on `main`.

The normal workflow is:

1. bootstrap offline GeoNames data,
2. inspect the inbox with `diagnose-inbox` or the Process Inbox panel,
3. organize all or selected DJI capture groups,
4. browse the library on the map,
5. correct, undo, rescan, warm, or repair the library with the UI and CLI tools.

## Shipped capability map

| Area | Current behavior |
| --- | --- |
| Ingest | Scans directory-aware DJI card layouts and groups primary media with DNG, LRF, SRT, hyperlapse frames, panorama tiles, and catalog data. The web UI can import all captures or selected groups. |
| Metadata | Uses ExifTool for photo/video metadata, parses DJI SRT GPS formats, detects video codecs, and reads DJI `MISC/*.db` star ratings without displaying them in the UI. |
| Location and time | Reverse-geocodes offline with GeoNames, resolves local capture time, prefers nearby named features when loaded, and can infer missing GPS from a time-adjacent capture. |
| Safe filing | Copies to a partial file, hashes and verifies it, atomically publishes it, and only then deletes sources. Capture groups are deleted companions-first and primary-last. |
| Special captures | Treats hyperlapses and panoramas as capture units with source-frame galleries. Panorama tiles get an instant collage and can optionally be stitched with Hugin. |
| Library browser | Provides clustered MapLibre browsing, vector/satellite/heatmap modes, viewport-scoped file lists, date grouping and sort, media filters, location search, and responsive desktop/mobile layouts. |
| Media viewer | Shows photos, browser-playable video, panorama heroes or tile galleries, local capture captions, keyboard navigation, and SRT flight paths synchronized to video playback. |
| Corrections | Supports map-click re-tagging for organized captures and map/place-search assignment for quarantined no-GPS captures. Both move full capture groups safely. |
| Operations | Includes undo, rescan, integrity verification, inbox diagnosis, derived-cache clearing, proxy warming, panorama re-stitching, and legacy collision recovery. |
| Access control | Read routes stay public. An optional admin password hides and protects management actions; login tokens are in-memory and failed logins are throttled. |

## Runtime architecture

### Python service and storage

- Python **3.13+**, packaged as `geosorter` and normally run through `uv`.
- FastAPI/uvicorn serves the API and the built React app from the same origin.
- The **index DB** stores captures, companions, move history, stitch state, and
  batch codec statistics.
- The **GeoNames DB** stores offline place/admin/country reference data.
- Both SQLite databases are local-disk operational state; the media library may
  live on a local disk, mapped drive, NAS, or UNC path.
- SQLite connections use WAL, foreign keys, and a busy timeout. Long-path helpers
  handle both `\\?\` drive paths and `\\?\UNC\...` paths.

### Frontend

- React 19 + TypeScript + Vite.
- MapLibre via `react-map-gl`, client-side clustering via `supercluster`.
- `@tanstack/react-virtual` bounds thumbnail rendering for large visible sets.
- Photo Sphere Viewer/Three.js are lazy-loaded only for stitched 360 panoramas.
- The map basemap is the main online dependency; media, indexes, geocoding, and
  management operations remain local.

### Worker model

- One shared destructive worker serializes organize, undo, re-tag, no-GPS
  assignment, and rescan work.
- A queued organize is allowed behind another organize, but dangerous cross-kind
  submissions receive a busy error instead of silently queueing.
- Panorama stitching uses its own single-worker pool.
- Post-organize cache warming uses another single-worker pool.
- Derived image generation is capped at four concurrent jobs; heavy HEVC proxy
  transcodes use a separate two-permit cap so they cannot starve thumbnails,
  previews, and posters.

## Current media and ingest contracts

### Supported capture shapes

- Normal DJI photos and videos.
- DNG/LRF/SRT companions.
- Hyperlapse render plus optional retained source frames.
- Panorama tile directories, always retained as a source-frame gallery.
- DJI MISC catalog databases, archived outside the library and restored by undo.

Same-named captures from different inbox directories remain separate groups.
Different content never overwrites an occupied destination; a suffix such as
`_2` or `_3` is selected instead.

### Duplicates

Content already present in the library is a duplicate. With the default
`relocate_duplicates = true`, the whole incoming capture group is moved under
`<inbox>/_duplicates/` and recorded in an append-only `duplicates.log`. The
duplicates directory is excluded from later inbox scans. Setting the option false
restores skip-in-place behavior.

### No-GPS handling

A capture with no usable or inferred coordinate is filed under
`<library_root>/_no-gps/<date>/` with `status='quarantined'`. It is not shown on
the map, but it appears in the No-GPS panel. An administrator can preview one or
more quarantined captures and assign a coordinate by:

- clicking the map, or
- selecting an offline GeoNames place-search result.

Assignment promotes the capture to `organized`, stamps `gps_source='manual'`, and
moves the primary and companions as one verified group.

## Current browser experience

- The file list contains captures inside the current map viewport.
- It groups by day, month, or year; sorts newest or oldest first; and filters
  photos, videos, panoramas, and hyperlapses.
- Desktop uses a resizable right rail. Below 1024 px it becomes a draggable,
  snap-height bottom sheet over a full-screen map.
- The Locations panel searches the places already represented in the library and
  flies the map to the selected bounds.
- The Trips panel derives trips client-side (runs of capture days at most an
  adjustable 1/2/3/7 idle-day gap apart, default 2), labeled by dominant place
  and day-resolution date range; picking one applies the trip's date range as
  the app-level filter (Clear chip, shareable `from`/`to` hash) and fits the
  camera to its captures. Public, like Locations.
- Initial library failures show a retryable error instead of an unexplained blank
  map.
- Keyboard focus indicators, reduced-motion support, accessible dialog semantics,
  Escape/arrow navigation, and dark native controls are implemented.

### Flight tracks

Videos with an SRT companion expose `has_track` in `/api/library`. The viewer can
request `/api/track/{file_id}`, draw the route on the map, and move the video into
a draggable picture-in-map player. Timestamped SRT samples synchronize a moving
drone marker with playback; the user can optionally make the map follow it.
Routes are downsampled to at most 500 points/samples while retaining endpoints.

### Panorama experience

Every panorama has an on-demand Pillow collage, so opening it does not wait for
Hugin. With Hugin installed, administrators can:

- generate or re-stitch one panorama,
- choose auto, equirectangular, cylindrical, or rectilinear projection,
- process the library-wide unstitched-panorama list.

The backend records whether the resulting hero is equirectangular or flat, and
the UI chooses the immersive sphere viewer or flat zoomable viewer accordingly.

## Derived assets and video playback

Cheap assets live in the local `cache_dir` tier:

- thumbnails,
- previews,
- video posters,
- panorama collages.

Large write-once assets live in `proxy_cache_dir`:

- HEVC-to-H.264 playback proxies,
- Hugin stitch heroes.

Freshness uses source mtime plus a source-size `.src` sidecar when present.
Library-mutating flows invalidate old and new cache keys when content moves or is
replaced. `clear-derived-cache` removes only the cheap local kinds and preserves
proxies and stitches.

H.264 video is served directly. HEVC video is transcoded to a cached H.264 proxy:

- `proxy_hwaccel = "auto"` uses NVENC when available and falls back to libx264,
- `"nvenc"` requires NVENC,
- `"none"` always uses libx264.

High-confidence permanent decode failures produce a cached placeholder for image
derivatives and HTTP 422 for unrenderable video. Transient generation failures
remain retryable and return HTTP 503.

## CLI surface

### Setup and serving

- `init-config`
- `set-admin-password`
- `bootstrap [--features]`
- `serve`
- `version`

### Organize and maintain

- `diagnose-inbox [--no-hash]`
- `organize [--dry-run] [--yes]`
- `undo [--batch ID]`
- `rescan [--dry-run]`
- `verify-library`
- `warm-proxies (--all | --batch ID) [--show-ffmpeg]`
- `clear-derived-cache`
- `restitch [--all] [--dry-run]`
- `recover-collisions [--dry-run]`

### Diagnostics and benchmarks

- `extract-test PATH`
- `geocode-test LAT LON`
- `proxy-bench FILE_ID`
- `stitch-bench FILE_ID`

## HTTP API surface

### Public reads

- library feed: `/api/library`
- originals and derivatives: `/api/media`, `/api/thumb`, `/api/preview`,
  `/api/poster`, `/api/video`
- special media: `/api/frames/{id}`, `/api/track/{id}`,
  `/api/collage/{id}`, `GET /api/stitch/{id}`
- inbox/no-GPS/place data: `/api/inbox`, `/api/inbox/list`,
  `/api/quarantine`, `/api/place-search`
- job status routes and `/api/auth`

### Management writes

When an admin password is configured, bearer authentication is required for:

- starting/cancelling organize,
- starting/cancelling undo,
- re-tagging,
- assigning no-GPS locations,
- rescanning,
- starting or re-starting panorama stitches.

Login and logout are `/api/login` and `/api/logout`. Read access to library
locations and media is intentionally not protected by the admin password.

## Operational boundaries and known limits

- The server binds to `127.0.0.1` by default.
- A non-loopback bind exposes media and GPS coordinates to that network even when
  management actions require an admin password.
- Tokens and job state are in-memory, so a server restart clears sessions and
  live job tracking.
- Hugin is optional and external; without it, panorama collage/tile browsing still
  works but stitched heroes cannot be generated.
- The map uses hosted tiles and therefore needs internet access for the basemap.
- Rescan removes stale DB rows for missing indexed media; it does not discover
  files copied directly into the library. New media should enter through organize.
- `diagnose-inbox --no-hash` is fast structural triage but cannot identify
  content duplicates.

## Verification snapshot

Verified on `main` on 2026-07-16:

- backend: **606 passed, 1 skipped**,
- frontend: **191 passed** across 25 test files,
- production frontend build: **passes** (with Vite's existing large-chunk warning),
- frontend lint: **not clean** — two existing `react-hooks/set-state-in-effect`
  errors (`Lightbox.tsx`, `useLibrary.ts`) and one
  `react-hooks/incompatible-library` warning for TanStack Virtual in
  `FileListPanel.tsx`.

Commands:

```bash
uv run pytest
npm --prefix frontend run test
npm --prefix frontend run lint
npm --prefix frontend run build
```
