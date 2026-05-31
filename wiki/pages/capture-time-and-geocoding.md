---
title: DJI Capture Time & Offline Geocoding
tags: [dji, metadata, timezone, geocoding, geonames, geosorter]
created: 2026-05-31
updated: 2026-05-31
sources: [task:h-geocode-tz-path]
---

# DJI Capture Time & Offline Geocoding

How geosorter turns a coordinate + a naive timestamp into a
`library/<City, Region, Country>/<YYYY-MM-DD>/...` destination. Two pieces of
non-obvious domain knowledge live here: how DJI stamps capture time, and how
GeoNames is queried for a place name offline. See also
[DJI SRT Telemetry Formats](pages/dji-srt-telemetry-formats.md) for where the GPS
coordinate itself comes from.

## Capture time is naive — and its meaning depends on the source tag

The timestamp ExifTool returns (`MediaMetadata.capture_ts_raw`) is a **naive**
wall-clock string with no offset. Its semantics differ by which tag it came from
(`MediaMetadata.capture_ts_source_tag`):

- **`QuickTime:CreateDate`** (DJI **video**) is **UTC** per the QuickTime spec.
  Format `"YYYY:MM:DD HH:MM:SS"` (note: ExifTool emits colon-separated dates, not
  ISO 8601).
- **`EXIF:DateTimeOriginal`** / **`EXIF:CreateDate`** (DJI **photo**) is the
  camera's **local wall-clock** — whatever the drone/controller clock was set to.

This drives the per-source policy in `tz_resolver.resolve_local_time`:

1. Derive the IANA zone from the GPS coordinate
   (`timezonefinder.timezone_at(lng=lon, lat=lat)` — **`lng` first**, an easy bug).
2. **UTC source** → attach UTC, then `astimezone(zone)` to get true local time.
   This is what makes the date folder correct across a midnight-UTC boundary:
   `02:30 UTC Jan 1` at UTC-7 becomes `19:30 Dec 31` local → `2023-12-31/` folder.
3. **Local source** → attach the zone to the naive wall-clock **without shifting**
   (assume the camera clock tracked the capture location's local time).

The local date/time (never UTC) is what foldering uses. A wall-clock that lands in
a DST fall-back overlap (the same local time occurs twice) is flagged
`tz_ambiguous` — detected by comparing the `fold=0` vs `fold=1` UTC offsets.

`tzdata` is a hard dependency: Windows ships no system IANA database, so
`zoneinfo` needs the pip-installed copy.

## Offline reverse geocoding (GeoNames, cities-only in Phase 0a)

`geocoder.reverse_geocode` resolves a coordinate to the nearest **populated place**
(`feature_class='P'`; parks/peaks/hydro classes L/T/H and the prefer-nearest-feature
heuristic are deferred to B5):

- **Bounding-box pre-filter** narrows to a handful of candidates before exact
  ranking. Uses the SQLite **R-tree** when present, else a columnar `(lat, lon)`
  index — auto-detected by probing `sqlite_master` (not by trusting config).
- The longitude half-width is widened by **`1/cos(lat)`** so the box stays roughly
  square in real distance; a degree of longitude shrinks toward the poles, and a
  fixed-degree window would otherwise miss the true-nearest city at high latitude.
- Exact **Haversine** distance ranks the candidates; nearest wins.
- The `"City, Region, Country"` display string is built with a **LEFT JOIN** to
  `admin1_codes` (`country_code || '.' || admin1_code`, e.g. `US.CO`) and
  `country_info`. LEFT (not inner) JOIN matters: some places — capitals especially
  — lack admin codes, and an inner join would drop the row entirely.

### geonameid is canonical; place_string is display-only

The library stores the stable **`geonameid`** as the key. The human `place_string`
is display-only and may drift (GeoNames updates, sanitizer retuning) — keying the
folder structure on it would bifurcate the library on any data refresh. Results are
cached in `geocode_cache` keyed on coordinates rounded to 4 decimals (~11 m).

### Two-database split (decision D24)

The `geonames` reference data and the `geocode_cache` live in **separate** SQLite
databases (both off `library_root`). `reverse_geocode` therefore takes the geonames
connection and the cache connection independently — it reads geonames and writes
the cache only when a cache connection is supplied.

## Windows-safe foldering

The place string is sourced from GeoNames `ascii_name` (already transliterated),
then sanitized for NTFS: strip `<>:"/\|?*` + control chars, NFC-normalize, drop
trailing dots/spaces, escape reserved device names (`CON`/`PRN`/`AUX`/`NUL`/
`COM1`-`COM9`/`LPT1`-`LPT9`), truncate the city to ≤40 chars, and fall back to the
`geonameid` when the name sanitizes to nothing. Absolute paths get the `\\?\`
long-path prefix so a deep `library_root` survives the legacy 260-char limit.
