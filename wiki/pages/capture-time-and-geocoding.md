---
title: DJI Capture Time & Offline Geocoding
tags: [dji, metadata, timezone, geocoding, geonames, geosorter]
created: 2026-05-31
updated: 2026-07-16
sources: [task:h-geocode-tz-path, task:h-feature-geocoding]
---

# DJI Capture Time & Offline Geocoding

How geosorter turns a coordinate + a naive timestamp into a
`library/<City, Region, Country>/<YYYY-MM-DD>/...` destination. Two pieces of
non-obvious domain knowledge live here: how DJI stamps capture time, and how
GeoNames is queried for a place name offline. See also
[DJI SRT Telemetry Formats](dji-srt-telemetry-formats.md) for where the GPS
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

## Offline reverse geocoding (GeoNames)

`geocoder.reverse_geocode` resolves a coordinate to a place — the nearest populated
place by default, or a nearby named feature when feature data is loaded (see the
prefer-nearest-feature heuristic below):

- **Bounding-box pre-filter** narrows to a handful of candidates before exact
  ranking. Uses the SQLite **R-tree** when present, else a columnar `(lat, lon)`
  index — auto-detected by probing `sqlite_master` (not by trusting config). The
  candidate set spans all classes (cities `P` plus features `L`/`T`/`H`); the
  heuristic splits them downstream.
- The longitude half-width is widened by **`1/cos(lat)`** so the box stays roughly
  square in real distance; a degree of longitude shrinks toward the poles, and a
  fixed-degree window would otherwise miss the true-nearest city at high latitude.
- Exact **Haversine** distance ranks the candidates; the prefer-nearest-feature
  rule (`_choose`) picks the winner.
- The `"City, Region, Country"` display string is built with a **LEFT JOIN** to
  `admin1_codes` (`country_code || '.' || admin1_code`, e.g. `US.CO`) and
  `country_info`. LEFT (not inner) JOIN matters: some places — capitals especially
  — lack admin codes, and an inner join would drop the row entirely.

### Prefer-nearest-feature heuristic (Phase 0b, B5)

Beyond populated places (`feature_class='P'`, from `cities500`), the geonames DB can
also hold named **L** (parks/areas), **T** (terrain/peaks), and **H** (hydro)
features. These come from the much larger `allCountries` dump and load **opt-in** via
`bootstrap --features` (the default `bootstrap` stays cities-only and fast). Only a
curated allowlist of feature *codes* is kept (`DEFAULT_FEATURE_CODES` in
`geonames_loader.py`) so wilderness captures fold under a meaningful name instead of
every creek and hillock. `reverse_geocode(..., feature_proximity_km=5.0)` then picks
by priority:

1. the nearest L/T/H feature **if within `feature_proximity_km`** → `nearest_feature`
2. else the nearest populated place (`P`) → `nearest_city`
3. else the nearest feature even beyond the radius (no city in range) → `nearest_feature`
4. else → `fallback`

So a named feature beats a marginally-closer town when within the radius
(feature-wins-if-≤, not strictly-closer); the default radius is 5.0 km. The chosen
path is recorded in `geocode_confidence`.

**Edge-of-feature limitation.** GeoNames features are point *centroids*, not
polygons, so "inside a park" is approximated by centroid distance. A small named peak
near the query can win over a large park whose centroid is far (observed: a point
inside Rocky Mountain NP resolved to *Mount Lady Washington* 1.9 km off rather than
the park centroid 14.8 km off). Both still yield meaningful wilderness names.

`geocode-test <lat> <lon>` prints the ranked candidate list (class, name, distance)
and the chosen result — the tool used to tune `feature_proximity_km`. Pass a negative
longitude after `--` (e.g. `geocode-test -- 40.4 -105.6`). Real-coordinate tuning at
5.0 km: downtown Denver → *Denver* (city), real mini4pro footage → *Vail Mountain*
(feature), Yosemite Valley → *Yosemite Valley* (feature).

### geonameid is canonical; place_string is display-only

The library stores the stable **`geonameid`** as the key. The human `place_string`
is display-only and may drift (GeoNames updates, sanitizer retuning) — keying the
folder structure on it would bifurcate the library on any data refresh. Results are
cached in `geocode_cache` keyed on coordinates rounded to 4 decimals (~11 m). The
cache key does **not** include `feature_proximity_km`, so retuning that knob after an
`organize` run requires clearing `geocode_cache` to re-evaluate seen coordinates.

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
