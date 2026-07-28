# Wiki Index

Pages are listed below by category.

## Sources

## Entities

## Topics
- [Current Project State](pages/current-project-state.md) — maintained snapshot of the shipped CLI, API, browser UI, worker model, safety contracts, security boundary, and operational limits
- [DJI SRT Telemetry Formats](pages/dji-srt-telemetry-formats.md) — how DJI encodes per-frame GPS in `.SRT` sidecars (bracket vs paren families, lon-first gotcha, null-island frames)
- [DJI MISC Catalog Databases](pages/dji-misc-catalog.md) — the `MISC/*.db` SQLite catalogs where DJI star ratings live (schema, live-vs-stale selection, the rename-suffix join, read-only/fail-safe parsing)
- [DJI Capture Time & Offline Geocoding](pages/capture-time-and-geocoding.md) — per-source UTC/local timestamp semantics, GPS-derived local time, GeoNames nearest-place lookup + prefer-nearest-feature heuristic (geonameid canonical), Windows foldering
- [Crash-Safe Move Engine & Organize Pipeline](pages/crash-safe-move-engine.md) — copy→verify→delete safety, idempotent recovery, group-atomic moves, collision/duplicate policy, quarantine and no-GPS promotion, undo, re-tag, rescan, and integrity verification
- [Backend, API, Web UI & Derived Assets](pages/phase1-backend-api.md) — current FastAPI/React contract: public library reads, admin-gated management, range-capable media, cache tiers/invalidation, HEVC proxies, background jobs, responsive browsing, and flight-track playback
- [DJI Panorama Stitching (Hugin)](pages/dji-panorama-stitching.md) — why OpenCV `cv2.Stitcher` fails on DJI sphere panos and the Hugin CLI pipeline works (`celeste` sky-masking, equirectangular projection, the measured ~7 min/434 MiB cost), the output-validity gate, and the lazy/cached/dedicated-pool/`stitch_status` architecture (B13)

## Analysis
