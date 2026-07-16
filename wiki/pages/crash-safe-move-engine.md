---
title: Crash-Safe Move Engine & Organize Pipeline
tags: [move-engine, organize, undo, retag, assignment, inference, crash-safety, sqlite, geosorter]
created: 2026-05-31
updated: 2026-07-16
sources: [src/geosorter/move_engine.py, src/geosorter/organize.py, src/geosorter/undo.py, src/geosorter/retag.py, src/geosorter/setloc.py, src/geosorter/rescan.py]
---

# Crash-Safe Move Engine & Organize Pipeline

The `organize` pipeline is the main place geosorter does something irreversible:
it **deletes** a source only after a verified library copy exists. Everything
below exists to make that operation survivable across local disks, NAS/SMB shares,
interruptions, repeated runs, and capture groups with multiple physical files.

## The move state machine (`move_engine.py`)

Per physical file, in strict order:

1. **Recompute the source SHA-256 immediately before copying** — not at scan time;
   the file could change in between. Insert a `moves` row `status='pending'` with
   `source_path`, `dest_path`, `source_sha256`, `batch_id`.
2. **Copy to `<dest>.partial`** (a staging name), then hash the copy.
3. On hash **match**: `os.replace(partial, final)` (atomic), set `dest_sha256`,
   flip `status='copy_verified'`. The final file appears only once, complete.
4. **Delete the source**, then flip `status='source_deleted'`. This is the
   irreversible step.
5. On any **mismatch / disk-full / IO error**: set `status='failed'`, clean the
   `.partial`, and **delete no further sources** — the batch halts.

It is **always copy+verify+delete, never `os.rename`**: `library_root` may be a NAS
(cross-volume `rename` fails with `EXDEV`), and the hash verify is the entire safety
story — a rename would skip it.

The engine is split into two primitives so the orchestrator can control atomicity:
`copy_and_verify()` (steps 1–3, leaves the source intact) and `commit_delete()`
(step 4). `commit_delete` keys its row update on the **stored** `source_sha256`
(captured from `copy_and_verify`'s result), never a re-hash of the live file.

## Idempotency & crash recovery

The recovery key is `moves.UNIQUE(source_path, source_sha256)`. A re-run resumes
from whatever row it finds:

- `source_deleted` → already done, skip (and `is_already_moved()` short-circuits
  the whole capture group before re-extracting metadata).
- `copy_verified`, source still present (killed after verify, before delete) →
  the destination is already byte-trusted; just delete the source. **No re-copy.**
- `pending` (killed mid-copy) → the `.partial` is untrusted; clean it and redo.
  The source is always still present (delete only follows `copy_verified`), so
  recovery is safe.

## Group-atomic deletes (no split captures)

A capture = a primary plus its `.DNG`/`.LRF`/`.SRT` companions and, where
applicable, hyperlapse source frames or panorama tiles. The pipeline
**copies and verifies every file in the group first, and only then deletes the
sources — companions first, the primary last.** Consequences:

- A failure on any group member aborts before *any* source in that group is
  deleted, so you never get a primary in the library with its companion stranded
  in the inbox.
- Because the primary is deleted *last*, a `source_deleted` row on the primary is
  a reliable "this whole group is done" sentinel — used to skip completed groups
  on a recovery run. A crash mid-delete recovers exactly-once.

## Duplicates & collisions (dedup-then-suffix)

`files.dest_path` is `UNIQUE`. The current policy:

- A source whose **content hash already exists** in `files` (from a *different*
  source — a re-import) is a duplicate. With the default
  `relocate_duplicates=true`, the whole incoming group moves under
  `<inbox>/_duplicates/<original-relative-path>` and an append-only TSV entry is
  written to `_duplicates/duplicates.log`. Companions move first and the primary
  last. The directory is excluded from future inbox scans. Setting the option
  false restores skip-in-place behavior.
- A *different-content* file that resolves to an occupied `dest_path` is given a
  `_2`/`_3` suffix (checked against both the `files` table and the filesystem).

The dedup check excludes the file's own in-flight moves row, so a recovery run
never mistakes its own partially-committed work for a duplicate.

Same-stem captures from different inbox directories are grouped independently.
The move engine also refuses to publish onto a destination already associated
with a different source, including stale-row retry paths, so recycled DJI
filenames cannot silently overwrite unrelated bytes.

## Resilience, quarantine, codec stats, first-run gate

- **Capacity and transient IO protection**: organize checks required bytes plus
  `disk_margin_gb`, retries transient copy errors with exponential backoff, and
  re-checks free space while the batch is running.
- **Extraction resilience**: large scans rotate the ExifTool daemon after
  `extract_chunk_size` groups. A repeatedly unreadable file is quarantined after
  `extract_max_failures` rather than aborting every later capture.

- **Quarantine**: a file with no GPS (`lat`/`lon` None) *or* no resolvable local
  date routes to `library_root/_no-gps/<date>/`, where `<date>` is the capture
  timestamp's date if parseable, else the file's mtime. `files.status='quarantined'`,
  geo columns NULL. It still moves through the same crash-safe engine.
  - Note: a video with no *embedded* GPS but a GPS-bearing `.SRT` sidecar
    **organizes** (B2 recovers GPS from the SRT) — it is not quarantined.
- **Codec stats**: a per-batch `codec_stats` row tallies h264 / h265 / unknown
  across **videos only** (photos have no codec). It supports proxy planning and
  diagnostics; HEVC playback proxies are now implemented.
- **First-run gate** (decision D22): the first destructive `organize` on a new
  library (detected by zero `moves` rows) prints source→dest→count and requires an
  explicit confirm defaulting to **No**; `--yes` bypasses it. `--dry-run` performs
  zero filesystem and zero DB writes but prints the identical summary.

## Time-clustered neighbor-GPS inference (`inference.py`)

To rescue captures that would otherwise quarantine for a missing GPS lock,
`run_organize` runs in **two passes**:

1. **Extract-all** — every capture group's metadata is read up front (groups a
   prior run already moved are skipped), releasing the ExifTool daemon before any
   move. `_infer_batch` then builds a **within-run pool** of
   `(idx, timestamp, coord)` and calls `inference.infer_locations`.
2. **Move-all** — each group is geocoded and group-atomically moved (the cancel
   predicate is polled here, between groups, preserving the documented contract).

`infer_locations` is a **pure** function: a no-coordinate but timestamped capture
borrows the `(lat, lon)` of the **nearest-in-time** capture that *has* a
coordinate, but only within `cfg.inference_max_gap_minutes` (default 30). The
borrowed coordinate is stamped `gps_source='inferred'` and flows through
tz→geocode→path exactly like real GPS, so the file organizes (and the map UI marks
it distinctly) instead of quarantining. Out-of-window or timestamp-less captures
are omitted → they still quarantine.

Design choices (all deliberately conservative):
- **Within-run only** — the pool is the current scan, never the existing index.
- **Clusters on the raw naive `capture_ts_raw`** (same clock for sources and
  targets). A cross-media photo (local wall-clock) vs video (UTC) match is skewed
  by the local UTC offset and usually falls outside the window — so the worst case
  is a *missed* match (→ quarantine), never a *wrong* far-off location.
- **No schema change** — `files.gps_source` already enumerated `'inferred'`.

## `verify-library`

Recomputes the on-disk SHA-256 of every organized destination and compares it to
the verified `moves.dest_sha256` (for `copy_verified`/`source_deleted` rows) to
detect post-deletion bit-rot — the safety net for an archive whose only copy now
lives in the library.

## Undo a batch (`undo.py`)

Undo reverses the most recent `organize` batch (or a given `--batch`): it moves
each filed library file **back** to its original inbox `source_path` and removes
the batch's index rows. The `moves` log is the substrate — it already records, per
file, the original `source_path`, the `dest_path`, and the verified
`source_sha256` (which, because copy was byte-verified, equals the library copy's
hash).

The reverse move mirrors the forward discipline exactly — **copy library →
`<source>.partial`, verify by hash, `os.replace` into the inbox, then delete the
library copy** — and likewise never `os.rename`s (cross-volume safe). It is a
*bespoke* path, deliberately **not** a reuse of `copy_and_verify` (that would write
forward-looking `moves` rows with the library file as the "source") and **not** a
new `moves.status='undone'` (the status `CHECK` can't be altered without a full
SQLite table rebuild).

**Idempotency is by disk state, not a status flag.** `_reverse_one` decides from
what is actually on disk, so a crash mid-undo *or* a leftover from a mid-organize
abort recovers the same way. For each row (both `source_deleted` and the rarer
`copy_verified` flow through one unified path):

- **library copy gone, source restored with matching hash** → already done (a prior
  partial run); just drop the rows.
- **library copy gone, source absent** → `missing` (cannot reverse); source occupied
  by *different* content → `conflict`.
- **source path occupied, hash matches** → our own restored file (crash between
  `os.replace` and the delete); finish by deleting the library copy + rows.
- **source path occupied, hash differs** → a *different* file the user dropped there:
  **skipped and reported in `conflicts`, never clobbered.**
- otherwise → the normal reverse move above; a verify mismatch records a `failure`
  and keeps both the library copy and the row (nothing destroyed).

**Row cleanup** is incremental and crash-safe (committed per file): dropping a
primary's `moves` row also deletes its `files` row, which cascades
`file_companions` (FK `ON DELETE CASCADE`, `foreign_keys=ON`); companion `moves`
rows (`file_id` NULL) delete by id. The per-batch `codec_stats` row is dropped only
once no `moves` or `files` rows remain for the batch — so a partial undo (some files
conflicted) correctly leaves the still-filed files indexed.

Undo is exposed as the `undo` CLI verb (confirm-gated) and as a cancellable
background job (`POST /api/undo`) that **shares the single-worker executor with
`organize`**, so the two destructive passes are mutually exclusive. `cancel` is
polled between files; remaining rows are left for a clean resume. Derived assets
are regenerable, and later writes invalidate the old/new cache keys so a reused
path cannot retain stale content.

## Manual re-tag (`retag.py`)

`retag_file(cfg, file_id, lat, lon)` re-files an already-**organized** capture to a
map-clicked coordinate (the map UI's "Re-tag location" → click). It re-geocodes the
coordinate, recomputes the local date/time from the file's **stored**
`capture_ts_utc` against the *new* timezone (`tz_resolver.local_time_from_utc`),
recomputes the destination, and physically relocates the primary + companions —
then marks the `files` row `gps_source='manual'`. This path corrects an already
organized GPS or inferred pin. Quarantined captures use the assignment workflow
below.

The move is a **library→library** variant of the same crash-safe discipline — a
*bespoke* path like undo's (not `move_engine`'s inbox→library primitives), and
**group-atomic**: `_relocate` copies every changed file to `<new>.partial`, verifies
it against the stored `dest_sha256`, and `os.replace`s it into the new path — for
*all* files — before the single index commit; only then are the old copies deleted.
So a verified copy always exists at the new path before the old is removed (no data
loss), and a new path occupied by *different* content is never clobbered (hash
mismatch → `status='failed'`, nothing destroyed). Idempotent by disk state: a new
path already holding the file's own verified bytes is accepted as a resume.

`new_dest` is disambiguated by `_resolve_collision` (a `_2`/`_3` suffix keyed on a
*different* `files.dest_path` owner) **before** the move, because that column is
`UNIQUE` — without it a re-tag onto a path another capture already holds would copy
the bytes and then fail the `UPDATE` with an `IntegrityError`, orphaning the copy.
The index update (one commit) rewrites the primary's `files` row (geo columns,
`gps_source='manual'`, `capture_ts_local`, `local_date`, `dest_path`, `filename`),
each `file_companions.dest_path`, and each `moves.dest_path` (the verified
`dest_sha256` is unchanged — the bytes did not). "No move needed" is decided by the
recomputed destination equalling the current one, not by coordinate equality.

Re-tag runs as a background job (`POST /api/retag`) on the shared single-worker
destructive executor. It has **no cancel** — a single-capture atomic move has
nothing to interrupt. The move invalidates both the vacated and newly written
derived-cache keys.

## Assign a location to quarantined media (`setloc.py`)

The No-GPS workflow promotes one or more `status='quarantined'` captures into the
organized library:

1. the UI obtains a coordinate from a map click or offline `/api/place-search`,
2. `assign_locations` geocodes that coordinate once,
3. each file's raw capture time is re-extracted (quarantined rows do not retain
   enough timestamp data for this calculation),
4. local time and the final destination are computed for the selected coordinate,
5. the primary and companions move from `_no-gps` to the library using the same
   group-atomic relocation primitives as re-tag,
6. the row becomes `status='organized'` with `gps_source='manual'`.

Undated captures fall back to file mtime. The operation runs through
`POST /api/assign-location` on the shared destructive worker and reports progress
per capture, not per physical companion file.

## Rescan stale index rows (`rescan.py`)

`rescan` reconciles the index with disk when media was moved out of the library by
hand. A missing primary causes the capture's DB rows to be pruned; a missing
companion with a present primary is warned about but retained. Stranded companions
are reported and never deleted.

Rescan mutates the index DB only. It does not delete/move media and does not
discover files copied into the library outside organize. The CLI supports a dry
run, and the web route uses the shared destructive worker.
