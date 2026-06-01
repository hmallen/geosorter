# Wiki Log

Chronological record of wiki operations.

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
