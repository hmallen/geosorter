# DJI Special Captures: MISC, Hyperlapse, Panorama — Results

## Topic
Three under-handled DJI ingest cases, confirmed against a real capture tree
(`Z:\drones\ingest\082924`) and refined through six-lens expert analysis plus
empirical verification:

1. **MISC metadata** — DJI writes a `MISC/` sibling to `DCIM/` holding SQLite
   catalog DBs (`dji_finfo.db`, `FC8482.db`). Today every non-DJI-named inbox file
   is scanned but never grouped, so MISC strands in the inbox indefinitely.
2. **Hyperlapse** — a rendered `DCIM/DJI_<ts>_<counter>_D.MP4` produced from
   250–350 source frames in `DCIM/HYPERLAPSE/001_<counter>/`. The render usually
   has **no GPS** (no `.SRT`); the frame folder links only by counter and the frame
   names don't match the grouper's regex, so frames are invisible and the render
   tends to quarantine.
3. **Panorama** — `DCIM/PANORAMA/001_<counter>/` holds only source frames
   (`PANO_NNNN.JPG`); DJI leaves no stitched composite and there is no DCIM primary.

## Key Features
- **Schema-migration runner** (`db.py`) so new columns can land on existing installs
  without `OperationalError` — the unanimous prerequisite.
- **Directory-aware ingest**: a pre-scan that routes flat-DCIM / HYPERLAPSE /
  PANORAMA / MISC instead of silently dropping non-DJI names.
- **Hyperlapse handling**: frames travel with the render as a new companion type
  (linked by folder-counter == video-counter + an mtime guard); GPS borrowed from
  frame EXIF when the render lacks it; browsable frame gallery in the lightbox;
  retain-by-default with a config toggle.
- **MISC ratings**: recover the user's in-app star ratings (the only place they
  exist) into a `files.star_rating` column, surfaced in the map UI.
- **Panorama as a capture unit**: its own `panorama` group (first frame as primary),
  GPS from frame EXIF, browsable frame gallery; a true stitched composite is a
  separate, deferred spike.

## Justification
- **MISC fixes a real leak**: non-DJI files are scanned by `organize` but ignored by
  `grouping.group_companions`, so they sit in the inbox forever. Empirically, star
  ratings live **only** in the catalog DB — `exiftool` on a starred render returns no
  `XMP:Rating` — so without parsing them, the user's curation is silently lost on
  import.
- **Hyperlapse fixes an active correctness bug**: SRT-less render videos have no GPS
  and currently quarantine; their frames strand separately. Verified that frames
  carry EXIF GPS, so borrowing it lands the render on the map and keeps the source
  with it.
- **Panorama**: frames carry GPS and organize today as 35 ungrouped singles; modeling
  them as one capture unit with a gallery gives a coherent, reliable experience now,
  while the unreliable/heavy stitching (OpenCV on sphere panos, no DJI lens
  calibration) is deferred behind a quality spike.

## Scope Definition
**In scope:**
- Schema-migration runner + the columns `capture_kind`, `frame_count`, `star_rating`.
- Directory-aware ingest pre-scan (HYPERLAPSE / PANORAMA / MISC recognition).
- Hyperlapse: counter+mtime link, frame companions, retain toggle, GPS-borrow with a
  distinct `gps_source`, frame gallery.
- MISC: schema-tolerant, read-only, fail-safe **ratings-only** parser → `star_rating`,
  surfaced in GeoJSON + file list; preserve the original DBs.
- Panorama: `panorama` capture unit (primary = `PANO_0001.JPG`), frame gallery,
  distinct map pin.

**Out of scope (now):**
- Camera-settings ingestion (ISO/shutter/aperture/ND/EV) — no UI consumer (YAGNI).
- OpenCV / true panorama stitching — deferred to a spike-gated follow-up.
- Reading the catalog `image_info_table` EXIF/thumbnail BLOBs.
- Retroactive reprocessing of already-quarantined hyperlapses (user can `undo` + re-run).

## Feature Details

### Schema-migration runner (B9a)
`db.py` is `SCHEMA_VERSION=1` with `CREATE TABLE IF NOT EXISTS` only. Add
`migrate_index_schema(conn)`: `PRAGMA table_info` to detect missing columns, guarded
`ALTER TABLE ... ADD COLUMN` (catch "duplicate column"), then stamp `schema_version`
**only after all ALTERs are confirmed** (a version-stamp-before-columns crash would
silently brick the DB). Mirror the new columns into `_INDEX_SCHEMA` so fresh installs
are v2 at creation. Move the `init_index_schema` call out of the per-request
`_index()` into `create_app` startup to close the WAL multi-connection race. Columns:
`files.capture_kind TEXT`, `files.frame_count INTEGER`, `files.star_rating INTEGER`
(all nullable; existing rows read as `NULL`).

### Directory-aware ingest + Hyperlapse (B10)
A pre-scan partitions inbox paths by top-level DCIM subdirectory (flat /
`HYPERLAPSE/001_<counter>/` / `PANORAMA/001_<counter>/` / `MISC/`); frame paths are
stripped from the flat `group_companions` so they don't become quarantined singles,
and `inbox.count_inbox` moves in lockstep. Unclaimed paths are surfaced as warnings,
never dropped. Hyperlapse: the folder `<counter>` matches the render video's
`_<counter>_` token, with an **mtime-proximity secondary guard** and a graceful
no-match fallback (orphan group, never wrong-link or raise). Frames attach as a new
`hyperlapse_frame` companion type in a subfolder beside the render; the render is the
only map/list entity, with a "view source frames" gallery/scrubber in the lightbox.
When the render lacks GPS, borrow from the first GPS-bearing frame's EXIF and record a
distinct `gps_source='hyperlapse_frame'` rendered as reliable (not amber). Frames are
retained by default with a `retain_hyperlapse_frames` config toggle; the organize
summary reports the frame disk cost. The 300-file group preserves the
copy→verify→delete group-atomic invariant (verified safe: per-file `moves` rows +
primary `source_deleted` sentinel; companion `moves.file_id IS NULL` so undo cascades
only via the primary).

### MISC ratings (B11)
A new schema-tolerant `misc_parser.py`: open the catalog DB read-only
(`file:...?mode=ro&immutable=1`), run `PRAGMA integrity_check`, read only scalar
columns (`gis_info_table.file_name`, `star`), never touch the EXIF BLOB, and wrap the
whole parse in `try/except` so it can never abort the crash-safe move path. Select the
live catalog DB by **basename-matching** inbox files (pick the DB with the most
matching rows; mtime/same-scan-dir guard; ambiguous collision → no-match), since
`FC8482.db` matched this card while `dji_finfo.db` was a stale older session. Write
results to `files.star_rating`; surface it in `/api/library` GeoJSON and the
`FileListPanel`, distinguishing `null` (never rated) from `0`. Preserve the original
DBs; never serve `.db` via `/api/media` (belt-and-suspenders extension block; the DBs
also live outside `library_root` by design). Depends on B9a only → may run in
parallel with B10.

### Panorama capture unit + gallery (B12)
The pre-scan (from B10) routes `PANORAMA/001_<counter>/` as a `capture_kind='panorama'`
group; `PANO_0001.JPG` is the primary (preserving `files.dest_path`/`sha256`
invariants), the rest are `panorama_frame` companions. GPS comes directly from the
primary frame's EXIF (`gps_source='exif'`). The map shows one pin (distinct
`.pin--panorama` style + legend); the lightbox shows a swipeable frame gallery. No
OpenCV dependency.

### Panorama stitch spike (B13 — deferred)
A quality spike: run OpenCV `Stitcher` against real DJI sphere panos, measure success
rate / quality / memory / time. Go/no-go; if go, add `derived.panorama_stitch()`
(optional `[panorama]` extra, lazy/cached, downsampled, Pillow-verified inputs,
subprocess+timeout, never warped-as-primary) + a `/api/stitch/{id}` route. Gated on
B12 + a real pano; the migration runner makes adding a `stitch_status` column trivial
at that point (none is added speculatively now).

## Reusable Code
- `move_engine.py:101` `copy_and_verify` / `:142` `commit_delete` — crash-safe
  group-atomic moves work unchanged for 300-file companion sets.
- `grouping.py:65` `group_companions` / `:35` `_DJI_RE` — the flat fast-path is kept;
  the directory pre-scan wraps it (frame paths stripped before it runs).
- `organize.py:91` `_companion_dest` — companion destination logic; a subfolder
  variant is added for frames (leave the original intact — `retag.py` uses it).
- `derived.py:78` `_resize_jpeg` / `:30` `_cache_path` / `:56` `_atomic_write` — frame
  thumbnails/gallery reuse these as-is; a future stitch reuses the cache scaffolding.
- `inference.py:40` `infer_locations` — no-GPS fallback path already exists; hyperlapse
  borrow is a direct frame-EXIF read, distinct provenance.
- `tz_resolver.py`, `geocoder.py`, `pathing.py` — reused unchanged for all kinds.
- `api.py:118` `/api/library` GeoJSON + `_safe_path:103` — additive properties
  (`capture_kind`, `star_rating`); frame files serve via the existing `/api/media`.
- `db.py:200` `init_index_schema` / `:192` `_stamp_version` — the migration runner
  slots in here; `file_companions.companion_type` has no CHECK constraint so new
  types are data-only.
- Frontend: `FileListPanel.tsx`, `Lightbox.tsx`, `types.ts` `FeatureProps`,
  `MapView.tsx` legend/pin styling — extended additively (gallery mode, badges,
  star widget, panorama pin).

## Deprecated / Dead Code
None. No module becomes obsolete. The only behavioral change is that `group_companions`
goes from "silently drops non-DJI names" (`grouping.py:74-77`) to "routes them via the
pre-scan / surfaces unclaimed as warnings."

## Implementation Plan
Granularity: **Option B (Balanced)** — 4 tasks + 1 deferred spike.

| Task | Name | Scope | Depends on | Success criteria |
|------|------|-------|------------|------------------|
| **B9a** | `h-schema-migration` | `migrate_index_schema(conn)` (PRAGMA table_info + guarded ALTER, stamp version only after all ALTERs; mirror columns into `_INDEX_SCHEMA`; `init` at `create_app` startup) + nullable `files.capture_kind`/`frame_count`/`star_rating`. No UI. | — | A v1 (B8-era) index DB opens under new code and gains all three columns losslessly with `schema_version=2`; fresh installs are v2 at creation; all existing tests stay green. |
| **B10** | `h-hyperlapse` | Directory-aware ingest pre-scan (HYPERLAPSE/PANORAMA/MISC routing; strip frames from flat grouper; `inbox.count_inbox` lockstep; unclaimed→warning) + hyperlapse: counter+mtime link with no-match fallback, `hyperlapse_frame` companions in a subfolder, retain-by-default + `retain_hyperlapse_frames` toggle (disk cost in summary), GPS-borrow → `gps_source='hyperlapse_frame'`, lightbox frame gallery + render badge. | B9a | Organize on the sample card files each hyperlapse render under a GPS-derived location (no quarantine), frames travel with it, gallery browsable, undo works at 300+ companions, no-match folder degrades gracefully. |
| **B11** | `m-dji-catalog` | Schema-tolerant read-only ratings parser (basename-match w/ most-rows + mtime/scope guard, scalar-only, try-wrapped) → `files.star_rating`; preserve DBs; surface `star_rating` in GeoJSON + `FileListPanel` (null vs 0); `.db` never served. | B9a (parallel with B10) | After organize with a MISC dir present, rated files expose `star_rating` in `/api/library` and the file list; a stale/corrupt/ambiguous catalog yields no ratings and never aborts the move. |
| **B12** | `m-panorama` | `panorama` capture unit (primary=`PANO_0001.JPG`, `panorama_frame` companions, GPS from frame EXIF) + distinct map pin/legend + lightbox frame gallery. No OpenCV. | B10 (reuses pre-scan + gallery) | The 35-frame `PANORAMA/001_0002/` organizes as one `panorama` capture with correct GPS, one map pin, browsable frames; undo works at 35 companions. |
| **B13** | `l-panorama-stitch-spike` | *(Deferred)* OpenCV `Stitcher` quality spike on real sphere panos → go/no-go; if go, `derived.panorama_stitch()` (optional `[panorama]` extra, lazy/cached, downsampled, Pillow-verified, subprocess+timeout, never warped-as-primary) + `/api/stitch/{id}`. | B12 + a real pano | Spike report with success-rate/quality/memory/time; a documented go/no-go; if go, stitched composite serves and falls back to gallery on failure. |

Ordering: **B9a → { B10 ∥ B11 } → B12 → [later] B13.**

