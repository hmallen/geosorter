---
title: DJI MISC Catalog Databases
tags: [dji, metadata, sqlite, ratings, geosorter]
created: 2026-06-04
updated: 2026-06-04
sources: [task:m-dji-catalog-ratings]
---

# DJI MISC Catalog Databases

A DJI card writes a `MISC/` directory as a sibling of `DCIM/` holding SQLite
catalog databases (e.g. `FC8482.db`, `dji_finfo.db`) plus `IDX/` and `THM/`
thumbnail/index caches. The catalog DB is where the DJI app records per-file
metadata — and, critically, the user's **in-app star ratings**, which live
**nowhere else**: the media files themselves carry no `XMP:Rating` (verified with
exiftool on a starred render). Without parsing the catalog, the user's curation is
silently lost on import.

## Schema (the parts that matter)

Verified against two real cards (`Z:\drones\ingest\082924\MISC`):

- **`gis_info_table`** — one row per card file. Relevant scalar columns:
  - `file_name TEXT` — a **full Android SD-card POSIX path**, not a basename:
    `/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_20240825165234_0001_D.MP4`. Take
    `os.path.basename` to match against organized files.
  - `star INT` — the rating, `0..5`. `0` means unrated (or rated zero); real cards
    are mostly `0` with a few rated rows.
- **`image_info_table`** — holds an `exif BLOB` (and thumbnail offsets). **Never
  read this.** Ratings need only the two scalar columns above; touching the BLOB is
  unnecessary and a needless decode-attack surface.
- Other tables (`video_info_table`, `mtime_table`, `version_table`, …) carry camera
  settings (ISO/shutter/ND/EV, beauty filters, …) — out of scope; no UI consumer.

Both real DBs passed `PRAGMA integrity_check`, but the parser must not assume that.

## Live vs. stale catalogs

A card can carry **multiple** catalog DBs, including stale ones from older sessions
on a different physical card. On the sample card:

- `FC8482.db` (mtime 2024-09-05, 225 rows) — **this** card: `file_name`s are modern
  `DCIM/DJI_001/...` / `DCIM/PANORAMA/001_0002/...`, and 3 rows have `star=1`.
- `dji_finfo.db` (mtime 2024-05-15, 54 rows) — a **stale older card**: every
  `file_name` is `DCIM/100MEDIA/DJI_00XX.MOV` (classic naming) and matches nothing
  on this card.

So the live catalog is chosen by **basename overlap** against the files actually
being organized — the stale DB scores zero and is rejected. A top-score tie or zero
overlap yields *no* attribution (never guess), so a stale/ambiguous catalog can
never mis-rate a file.

## The rename-suffix join gotcha

geosorter renames organized files to `<YYYY-MM-DD>_<HH-MM-SS>_<DJI_orig>.<ext>` and
the index `files` table stores only that renamed `filename` (no original-name
column). The catalog keys are the **original** DJI basenames, which are a *suffix*
of the renamed dest. So the join is `dest_filename.endswith(catalog_basename)`
(longest match wins), not equality. A rare `_2`/`_3` collision-suffixed dest
(`..._dji_0001_2.mp4`) no longer ends with `dji_0001.mp4` and simply misses its
rating — fail-safe (absent, never wrong).

## How geosorter uses it

`src/geosorter/misc_parser.py::read_ratings` opens the DB read-only and immutable
(`file:...?mode=ro&immutable=1`), runs `PRAGMA integrity_check`, reads only
`gis_info_table.file_name`/`star`, and is fully `try/except`-wrapped → `{}` on any
error, so a bad catalog can never abort the crash-safe move path.
`select_catalog` does the basename-overlap pick. `organize._apply_catalog_ratings`
(a post-move step) writes the chosen ratings to `files.star_rating`, then archives
**every** `MISC/*.db` (crash-safe, via the move engine) to
`<index_db_dir>/catalogs/<batch_id>/` outside `library_root` and deletes the inbox
copy — preserving the originals while decluttering the inbox. The archive is a
`moves` row with `file_id` NULL, so `undo` reverses it. `.db` files are never served
via `/api/media`. Ratings are still carried in the `/api/library` GeoJSON
(`star_rating`) and stored in the DB, but as of `m-hide-star-rating-ui` they are no
longer displayed in the frontend (the read-only star widget was removed; the data is
retained for future use). See also
[DJI SRT Telemetry Formats](pages/dji-srt-telemetry-formats.md) for the other
DJI-format reader.
