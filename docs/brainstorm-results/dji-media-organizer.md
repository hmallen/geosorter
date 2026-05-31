# DJI Media Organizer — Results

## Topic

A local, single-user (Windows) system that automatically organizes geotagged DJI
drone media by **location and date**, then browses it through a polished,
map-centric web frontend. The user drops files into an inbox, triggers a
button/CLI, and the system extracts GPS + timestamp metadata (photo EXIF, video
MP4 atoms, and `.SRT` sidecar telemetry), reverse-geocodes the location offline,
and moves/renames files into a structured library. The library is indexed in
SQLite and exposed on an interactive map where capture-location markers open the
associated photos/videos.

**Why:** Hunter has accumulated (and expects to keep accumulating) hundreds of
drone videos and thousands of drone photos. The goal is one repeatable
ingest → organize → browse workflow that turns a messy folder into a browsable,
location-aware library without manual sorting — entirely local, no cloud, no
third party learning flight locations.

## Key Features

- **Button/CLI-triggered ingest pipeline** — extract → geocode → organize → move,
  with a live no-UI CLI in the first milestones and a "Process Inbox" button later.
- **DJI metadata extraction** — pyexiftool (ExifTool daemon mode) for EXIF + MP4
  atoms, plus a custom parser for `.SRT` telemetry sidecars (older models / videos
  lacking embedded GPS).
- **Offline reverse geocoding** — GeoNames data in SQLite; place names from
  populated places first (Phase 0a), then parks/peaks/hydro features (Phase 0b).
- **GPS-derived local time** — true local capture date/time computed from the
  coordinate (offline `timezonefinder`), so date folders are correct across
  timezones and midnight boundaries.
- **Structured library** — `library/<Place>/<YYYY-MM-DD>/<YYYY-MM-DD>_<HH-MM-SS>_<DJI_orig>.<ext>`,
  with `.DNG`/`.LRF`/`.SRT` companions attached to their primary capture.
- **Crash-safe move engine** — per-file copy → recompute SHA-256 → verify →
  log → auto-delete source, with a 3-state status for atomicity, abort-on-mismatch,
  idempotent re-runs, and a first-run dry-run + confirmation gate.
- **Polished map viewer (Phase 1)** — MapLibre GL JS + supercluster clustering;
  click a marker → file list → lightbox (photos) / `<video>` player (H.264).
- **Library integrity** — `verify-library` command re-checks stored hashes to
  detect post-deletion destination bit-rot.

## Justification

The discussion + two rounds of six-specialist analysis produced strong, convergent
evidence for this design:

- **The core value is high and the friction reduction is extreme.** Today the
  SD-card → browsable library workflow is fully manual; the pipeline eliminates
  every manual step and the `<Place>/<Date>` tree is legible even in Explorer
  (User Perspective).
- **Python is the right home for the hard part.** DJI metadata extraction
  (maker notes, cross-generation embedded GPS, SRT) is ExifTool's domain, and the
  map+gallery UX is a web idiom — validating the FastAPI-backend + browser-SPA
  stack (Architecture, Code Impact).
- **Risk is front-loaded into the pipeline, so it is built and proven first.**
  The destructive move is the one irreversible action; phasing a headless CLI
  before any UI lets it be validated on real footage before UI investment, and
  gives auto-delete a validation runway (Scope, Critic).
- **The safety story holds up under adversarial review.** Auto-delete (the user's
  explicit choice for disk efficiency) is made survivable by per-file verify,
  abort-on-mismatch, idempotent recovery, a 3-state status for crash-atomicity,
  a first-run gate, and a `verify-library` bit-rot check (Critic, Risk).
- **Privacy and security defaults are sound for a personal GPS archive** —
  fully offline geocoding, server bound to `127.0.0.1` by default, ExifTool pinned
  ≥ 12.24, no-shell subprocess calls (Risk).

## Scope Definition

**In scope (this brainstorm → implementation phases 0a, 0b, 1, 2):**
- Local Windows, single user, local/NAS storage only — no cloud for media.
- DJI drones only (Mavic / Air / Mini / Phantom families).
- Photo + video organization by reverse-geocoded place and GPS-local date.
- SQLite index; map + gallery browsing; H.264 video streaming.
- Scale: hundreds of videos + thousands of photos (not millions).

**Out of scope:**
- Multi-user, authentication, remote/cloud hosting (LAN access is an opt-in flag only).
- Non-DJI cameras and arbitrary photo libraries.
- Editing / color-grading / cataloging beyond organize + browse.
- Polygon-accurate "inside this park" geofencing (GeoNames is point-centroid; the
  prefer-nearest heuristic is an approximation with a documented failure mode).
- Map-millions-of-points scale (no viewport/bbox marker endpoint until > ~50k points).

## Feature Details

### Ingest pipeline (CLI, Phase 0a/0b)
`click`-based CLI with `organize`, `organize --dry-run`, `bootstrap`
(GeoNames import), `geocode-test <lat> <lon>` (Phase 0b heuristic tuning), and
`verify-library`. The pipeline: scan inbox → group capture units + companions
(by original DJI base name + counter + time-proximity, before renaming) →
pyexiftool daemon extraction (GPS, timestamp, codec via `VideoCodecID`) →
SRT parse fallback → `timezonefinder` local-time derivation → GeoNames geocode →
path computation + Windows sanitizer → crash-safe move → quarantine no-GPS →
codec stats. Output is a rich grouped batch summary (per-place, file types,
quarantine count, codec stats) with per-file progress; `--dry-run` mirrors that
format with zero filesystem writes.

### Crash-safe move engine (Phase 0a)
Per file: insert `moves` row `status=pending` with the source SHA-256 (recomputed
immediately before copy) → copy → hash destination → on match flip to
`copy_verified` → delete source → `source_deleted`; on any mismatch / disk-full /
transport error set `failed`/`aborted`, **delete no further sources**, and clean
any partial destination file. Re-runs are idempotent on `(source_path AND sha256)`
— already-complete files are skipped (also the crash-recovery path). The moves-DB
lives on local disk (`moves_db_path` separate from `library_root`). First
destructive run on a new inbox/library requires an explicit confirm (showing
source → dest → count, default No). Pre-flight disk-space check before each batch.

### Reverse geocoding (Phase 0a cities → 0b features)
GeoNames loaded into SQLite with an R-tree virtual table (startup probe with a
columnar Haversine fallback). Phase 0a: cities500 + admin codes, nearest populated
place. Phase 0b: add feature classes L (parks/areas), T (terrain/peaks), H (hydro);
0.5° bbox pre-filter → ~20 candidates → prefer-nearest (a named feature wins when
closer than the nearest town, within a config-tunable `feature_proximity_km`).
`geonameid` is the stable canonical key stored in the index (`place_string` is
display-only) so GeoNames updates / heuristic retuning never bifurcate the library.
Place strings for filesystem use come from `ascii_name`, sanitized (illegal chars,
reserved names CON/PRN/…, NFC, ≤40-char truncate), falling back to `geonameid`.

### Map viewer (Phase 1)
FastAPI serves `organize`/`library` (full GeoJSON, loaded once — no bbox endpoint)/
`media` (range-request streaming, path-traversal-guarded) endpoints; bound to
`127.0.0.1` by default. React + Vite SPA renders MapLibre GL JS + supercluster;
marker → file list → lightbox (1080p photo) / `<video>` (H.264). Thumbnails
(512px, Pillow + `exif_transpose`) and video poster frames (ffmpeg) generated at
ingest. HEVC handling chosen from Phase 0 codec stats before this phase starts.

### Deferred polish (Phase 2)
Undo-last-batch UI (reads the `moves` log already populated in Phase 0), watchdog
inbox counter, time-clustered neighbor-GPS inference (`confidence: inferred` +
distinct markers), manual map-click re-tag, satellite (Esri) + heatmap toggles,
larger photo previews, HEVC→H.264 proxy transcoding (if not pulled into Phase 1).

## Library Leverage (buy-not-build)

This is a greenfield repo, so there is no existing code to reuse. The equivalent
analysis is which mature libraries to lean on rather than hand-roll:

- `pyexiftool` + ExifTool binary (≥ 12.24, daemon mode) — DJI metadata extraction.
- `timezonefinder` 6.x + `tzdata` (non-numba, instantiated once) — offline tz-from-coordinate.
- `Pillow` (`ImageOps.exif_transpose`) — photo thumbnails (Phase 1).
- ffmpeg / `ffprobe` binaries via list-form subprocess — poster frames, codec fallback.
- `click` — CLI; `sqlite3` + `zoneinfo` (stdlib) — index + tz conversion (no ORM).
- SQLite R-tree (bundled) — spatial geocoding index.
- Phase 1: `fastapi`/`uvicorn`, React + Vite, MapLibre GL JS, `supercluster` (via `react-map-gl`).
- **Dropped:** `reverse_geocoder` (cities-only; cannot satisfy the Phase 0b
  feature-class prefer-nearest requirement) → replaced by the custom GeoNames
  SQLite + R-tree approach.

The genuinely custom (highest-risk) code is: the DJI SRT telemetry parser (4+
format variants), the crash-safe move engine, and the GeoNames prefer-nearest
place-naming heuristic.

## Deprecated / Dead Code

None — greenfield project, first implementation. No existing application code
becomes obsolete.

## Implementation Plan

Chosen granularity: **Option B — Balanced** (functional-boundary split; each task
is a 1–2 session deliverable). Tasks feed the `task` protocol in order. TDD applies
to extraction, SRT parsing, geocode lookup, path computation, the sanitizer, and
the move-engine; it does not apply to scaffolding or the GeoNames bootstrap script.

| # | Task (suggested slug) | Scope | Definition of done |
|---|----------------------|-------|--------------------|
| B1 | `h-scaffold-schema-bootstrap` | Project scaffold, `config` (inbox/library/`moves_db_path`/geonames paths), `db` module (WAL + `synchronous=NORMAL` + startup `integrity_check`, conn-per-thread), full SQLite schema (`files` incl. `geonameid` + status fields, `file_companions`, `moves` with status enum, `geocode_cache`, `geonames`+rtree, `codec_stats`), GeoNames **cities** `bootstrap` verb (download/import + R-tree probe w/ columnar fallback, progress, disk pre-flight) | `bootstrap` loads cities500 + admin into SQLite with a working spatial index (or logged fallback); schema migrations create cleanly; config loads from `geosorter.toml` |
| B2 | `h-extract-srt-codec` | pyexiftool daemon extractor (GPS, timestamp, dimensions, `VideoCodecID`), **SRT parser spike** across ≥3 DJI format variants (log SRT-GPS vs EXIF-GPS), codec detection (`avc1`/`hvc1`, ffprobe fallback) | Extracts correct GPS+timestamp from a JPEG and an H.264 MP4 fixture; SRT parser returns correct first-fix GPS on ≥3 real variants; partial-parse flagged `gps_source=srt_partial`; ExifTool ≥12.24 startup check; all subprocess calls list-form |
| B3 | `h-geocode-tz-path` | Geocoder (cities prefer-nearest + `geocode_cache`), `tz_resolver` (timezonefinder + tzdata, once-per-run; `tz_ambiguous` via `fold`), path computation, Windows sanitizer (illegal/reserved/NFC/≤40, `ascii_name`→`geonameid` fallback), companion grouping (original base name + counter + <10s proximity, pre-rename) | Given metadata, produces correct `library/<City, Region, Country>/<YYYY-MM-DD>/<filename>`; tz-local date correct across a UTC-boundary fixture; sanitizer green against an edge-case name corpus; companions grouped to primary |
| B4 | `h-move-engine-cli` | Crash-safe move engine (insert `pending`+source SHA-256 recomputed pre-copy → copy → verify → `copy_verified` → delete → `source_deleted`; abort-on-mismatch, no further deletes, clean partial dest; idempotent re-run on `(source_path AND sha256)`; pre-flight disk space), quarantine router, codec-stats reporter, CLI (`organize`, `organize --dry-run`, `verify-library`), rich grouped batch summary + per-file progress, **first-run dry-run+confirm gate** → **Phase 0a done** | `organize` on real mixed footage (JPEG + H.264 + SRT video + no-GPS) produces correct structure; `moves` populated; quarantine populated; codec stats printed; re-run idempotent; simulated mid-flight kill → clean re-run (no double-delete/double-copy); first destructive run prompts for confirm |
| B5 | `h-feature-geocoding` | **Phase 0b** — GeoNames feature-class (L/T/H) loader, prefer-nearest heuristic, adopt `geonameid` canonical key end-to-end, `geocode-test <lat> <lon>` verb, tune `feature_proximity_km` on real coordinates, `geocode_confidence` field | Wilderness fixture resolves to a named feature (e.g. a park) while urban fixture still resolves to city; thresholds documented in config; Phase 0a tests stay green |
| B6 | `h-api-backend` | **Phase 1 backend** — FastAPI app (bind `127.0.0.1`, `--host` opt-in + warning), `organize` (ThreadPoolExecutor job + status), `library` GeoJSON (loaded once, no bbox), `media` (range-request streaming, path-traversal guard), thumbnails (Pillow + `exif_transpose`) + video poster frames (ffmpeg) at ingest; HEVC strategy chosen from Phase 0 codec stats | End-to-end API: trigger organize, fetch library GeoJSON, stream a DJI video with working seek, serve thumbnails; no path-traversal escape |
| B7 | `h-map-viewer` | **Phase 1 frontend** — React + Vite SPA, MapLibre GL JS + supercluster, marker/cluster → file list panel → lightbox (1080p photo) / `<video>` player; built against the B6 GeoJSON contract | Drop files → Process Inbox → markers on map → click marker → photos in lightbox, H.264 videos play inline |
| B8 | *(Phase 2, later)* | Undo-batch UI, watchdog inbox counter, neighbor-GPS inference, manual re-tag, satellite/heatmap toggles, larger previews, HEVC proxy (if deferred) | Broken into discrete tasks when Phase 1 ships |

**First task to start:** B1 (`h-scaffold-schema-bootstrap`). Spike-critical items
land in B2 (SRT parser) and B4 (move engine).
