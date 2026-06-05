# Wiki Index

Pages are listed below by category.

## Sources

## Entities

## Topics
- [DJI SRT Telemetry Formats](pages/dji-srt-telemetry-formats.md) — how DJI encodes per-frame GPS in `.SRT` sidecars (bracket vs paren families, lon-first gotcha, null-island frames)
- [DJI MISC Catalog Databases](pages/dji-misc-catalog.md) — the `MISC/*.db` SQLite catalogs where DJI star ratings live (schema, live-vs-stale selection, the rename-suffix join, read-only/fail-safe parsing)
- [DJI Capture Time & Offline Geocoding](pages/capture-time-and-geocoding.md) — per-source UTC/local timestamp semantics, GPS-derived local time, GeoNames nearest-place lookup + prefer-nearest-feature heuristic (geonameid canonical), Windows foldering
- [Crash-Safe Move Engine & Organize Pipeline](pages/crash-safe-move-engine.md) — the irreversible auto-delete made survivable: copy→verify→delete state machine, idempotent recovery, group-atomic deletes, dedup/collision policy, quarantine, first-run gate, verify-library, the B8 two-pass restructure + time-clustered neighbor-GPS inference, the B8 batch-undo reverse move, and the B8 manual map-click re-tag (library→library group-atomic move, gps_source='manual')
- [Phase 1 Backend — HTTP API Contract & Derived Assets](pages/phase1-backend-api.md) — the B6 FastAPI contract + B7 frontend: GeoJSON library feed, range-capable media serving + traversal guard, the on-demand HEVC→H.264 proxy + 1080p preview, lazy/atomic derived-asset cache, the cancellable single-worker organize job, and the React/MapLibre/supercluster SPA (OpenFreeMap tiles)

## Analysis
