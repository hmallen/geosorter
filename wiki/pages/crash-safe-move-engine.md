---
title: Crash-Safe Move Engine & Organize Pipeline
tags: [move-engine, organize, undo, crash-safety, sqlite, geosorter, phase-0a, phase-2]
created: 2026-05-31
updated: 2026-06-01
sources: [task:h-move-engine-cli, task:h-undo-batch]
---

# Crash-Safe Move Engine & Organize Pipeline

The `organize` pipeline (task B4) is the one place geosorter does something
irreversible: it **auto-deletes** each source file after copying it into the
library (decision D14 — the user's chosen policy for disk efficiency). Everything
below exists to make that delete survivable. This completes **Phase 0a**: a
headless `extract → geocode → local-time → path → move` pipeline.

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

A capture = a primary plus its `.DNG`/`.LRF`/`.SRT`/`_N` companions. The pipeline
**copies and verifies every file in the group first, and only then deletes the
sources — companions first, the primary last.** Consequences:

- A failure on any group member aborts before *any* source in that group is
  deleted, so you never get a primary in the library with its companion stranded
  in the inbox.
- Because the primary is deleted *last*, a `source_deleted` row on the primary is
  a reliable "this whole group is done" sentinel — used to skip completed groups
  on a recovery run. A crash mid-delete recovers exactly-once.

## Duplicates & collisions (dedup-then-suffix)

`files.dest_path` is `UNIQUE`. The policy (user's choice):

- A source whose **content hash already exists** in `files` (from a *different*
  source — a re-import) is **skipped and left in the inbox** (not deleted).
- A *different-content* file that resolves to an occupied `dest_path` is given a
  `_2`/`_3` suffix (checked against both the `files` table and the filesystem).

The dedup check excludes the file's own in-flight moves row, so a recovery run
never mistakes its own partially-committed work for a duplicate.

## Quarantine, codec stats, first-run gate

- **Quarantine**: a file with no GPS (`lat`/`lon` None) *or* no resolvable local
  date routes to `library_root/_no-gps/<date>/`, where `<date>` is the capture
  timestamp's date if parseable, else the file's mtime. `files.status='quarantined'`,
  geo columns NULL. It still moves through the same crash-safe engine.
  - Note: a video with no *embedded* GPS but a GPS-bearing `.SRT` sidecar
    **organizes** (B2 recovers GPS from the SRT) — it is not quarantined.
- **Codec stats**: a per-batch `codec_stats` row tallies h264 / h265 / unknown
  across **videos only** (photos have no codec). Feeds the eventual HEVC-handling
  decision.
- **First-run gate** (decision D22): the first destructive `organize` on a new
  library (detected by zero `moves` rows) prints source→dest→count and requires an
  explicit confirm defaulting to **No**; `--yes` bypasses it. `--dry-run` performs
  zero filesystem and zero DB writes but prints the identical summary.

## `verify-library`

Recomputes the on-disk SHA-256 of every organized destination and compares it to
the verified `moves.dest_sha256` (for `copy_verified`/`source_deleted` rows) to
detect post-deletion bit-rot — the safety net for an archive whose only copy now
lives in the library.

## Undo a batch (`undo.py`, task B8 — Phase 2)

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
polled between files; remaining rows are left for a clean resume. Orphaned
derived-cache entries (thumbs/previews/posters/proxies) are left as-is — they are
mtime-keyed and regenerate on demand.
