# Wiki Log

Chronological record of wiki operations.

## [2026-06-01] update | Crash-Safe Move Engine & Phase 1 Backend (B8 undo)
Documented the Phase 2 (B8) batch-undo feature from task h-undo-batch: added an
"Undo a batch" section to [Crash-Safe Move Engine](pages/crash-safe-move-engine.md)
(bespoke reverse copy→verify→delete, disk-state idempotency, skip-on-conflict,
incremental row cleanup + per-batch codec_stats drop) and extended
[Phase 1 Backend](pages/phase1-backend-api.md) with the `/api/undo*` endpoints and
the shared single-worker executor (organize/undo mutual exclusion). Updated the
index entry and both pages' front-matter.

## [2026-05-31] create | DJI SRT Telemetry Formats
Captured DJI `.SRT` telemetry domain knowledge from task B2 (h-extract-srt-codec):
bracket vs paren payload families, longitude-first ordering in `GPS(...)`,
null-island pre-lock frames, and partial-vs-absent GPS handling.

## [2026-05-31] create | DJI Capture Time & Offline Geocoding
Captured B3 (h-geocode-tz-path) domain knowledge: DJI naive timestamps and their
per-source semantics (QuickTime=UTC, EXIF=local), GPS-derived local-time policy +
UTC-boundary date crossing + DST `fold` ambiguity, GeoNames nearest-place lookup
(R-tree/columnar bbox with `cos(lat)` longitude correction, LEFT-JOIN place string,
`geonameid` canonical / `place_string` display-only, two-DB split D24), and the
Windows-safe foldering rules.

## [2026-05-31] create | Crash-Safe Move Engine & Organize Pipeline
Captured B4 (h-move-engine-cli) architecture, completing Phase 0a: the irreversible
auto-delete (D14) made survivable via the copy→`.partial`→verify→`os.replace`→delete
state machine, idempotent crash recovery keyed on `moves.UNIQUE(source_path, source_sha256)`,
group-atomic deletes (companions-first/primary-last, primary `source_deleted` = group-done
sentinel), dedup-by-hash-then-suffix collision policy, quarantine routing, video-only
codec stats, the first-run confirm gate (D22), and `verify-library` bit-rot detection.

## [2026-05-31] update | DJI Capture Time & Offline Geocoding (B5 feature geocoding)
Updated the geocoding section for B5 (h-feature-geocoding), Phase 0b: the
prefer-nearest-feature heuristic (`_choose` priority order, opt-in `bootstrap
--features`, curated `DEFAULT_FEATURE_CODES` allowlist over the `allCountries` dump,
`feature_proximity_km` default 5.0, `geocode_confidence` values
nearest_feature/nearest_city/fallback), the point-centroid edge-of-feature
limitation, the `geocode-test` verb, the geocode_cache key caveat, and the
real-coordinate tuning results (Denver→city, Vail Mountain/Yosemite→feature). No
schema migration. Added a key-decisions bullet.

## [2026-06-01] create | Phase 1 Backend — HTTP API Contract & Derived Assets
Captured B6 (h-api-backend) architecture, opening Phase 1: the FastAPI contract
B7 builds against (GeoJSON `/api/library` loaded-once feed of organized+geolocated
files; range-capable `/api/media` via starlette `FileResponse`; resolve()+
is_relative_to traversal guard; `/api/thumb`/`/api/poster`/`/api/video`; conditional
same-origin SPA mount), the loopback-default/no-auth security posture, the codec-stats
gate and resulting **on-demand HEVC→H.264 proxy** decision, the lazy + mtime-cached +
atomically-written (`mkstemp`→`os.replace`) derived-asset model under
`.geosorter-cache/` (keeping organize.py free of Pillow/ffmpeg), and the cancellable
single-worker `JobManager` (ThreadPoolExecutor max_workers=1, between-groups cancel
preserving group-atomicity; not FastAPI BackgroundTasks).

## [2026-06-01] update | Phase 1 Backend — HTTP API Contract & Derived Assets (B7 frontend)
Extended the page for B7 (h-map-viewer), completing Phase 1: added the new 1080p
`/api/preview` endpoint (1920px long-edge JPEG, lightbox) to the derived-asset family,
and a **Frontend SPA** section — the Vite + React + TS app (`react-map-gl`/MapLibre/
`supercluster`) that builds to `src/geosorter/webui` and is served same-origin by
`geosorter serve`. Captured the OpenFreeMap hosted-tiles choice (only online dep;
offline pmtiles deferred to Phase 2) and the client-side clustering design (whole
`/api/library` loaded once; `supercluster.getClusters(bbox, zoom)` returns only visible
clusters/points), with pure logic Vitest-tested and the UI verified by manual smoke.

## [2026-06-01] update | Phase 1 Backend — HTTP API Contract (B8 inbox counter)
Added the `GET /api/inbox` endpoint (task m-inbox-counter, Phase 2/B8): scan-on-request
`{files, captures}` counts for the next `organize` run — `files` = recursive file count
the pipeline scans, `captures` = DJI capture-group count (`group_companions`) it
processes; `{0,0}` for an unset/missing/empty inbox. Deliberately no `watchdog` observer
(drone inboxes are small; the frontend polls for a toolbar badge). Backed by the new
pure `geosorter.inbox.count_inbox`.

## [2026-06-01] update | Crash-Safe Move Engine & Organize Pipeline (B8 neighbor-GPS inference)
Documented the B8 two-pass `organize` restructure (extract-all → infer → move-all) and
the new pure `geosorter.inference` layer (task h-neighbor-gps-inference, Phase 2/B8): a
no-coordinate but timestamped capture borrows the location of its nearest-in-time
GPS-bearing capture within `inference_max_gap_minutes` (default 30), stamped
`gps_source='inferred'`, instead of quarantining. Clusters on raw naive `capture_ts_raw`
(conservative: cross-media UTC/local skew misses → quarantine, never a wrong location);
within-run pool only; no schema change (`files.gps_source` already enumerated `inferred`).

## [2026-06-01] update | Phase 1 Backend — HTTP API Contract (B8 gps_source property)
The `GET /api/library` GeoJSON feature `properties` now include `gps_source`
(`exif`|`srt`|`srt_partial`|`inferred`|`none`|null), so the B7 map UI can render
`inferred`-location markers with a distinct amber/dashed pin + legend (task
h-neighbor-gps-inference). Additive, non-breaking contract change.
