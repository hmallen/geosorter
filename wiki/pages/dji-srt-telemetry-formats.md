---
title: DJI SRT Telemetry Formats
tags: [dji, srt, metadata, gps, geosorter]
created: 2026-05-31
updated: 2026-07-16
sources: [src/geosorter/srt_parser.py, src/geosorter/api.py, frontend/src/flightTrack.ts, task:h-extract-srt-codec]
---

# DJI SRT Telemetry Formats

DJI drones write a per-frame flight-telemetry track into a subtitle (`.SRT`)
sidecar alongside each video. geosorter parses this to recover GPS when a video
lacks embedded (QuickTime atom) coordinates — common on several models. No
off-the-shelf library covers the payload: the `srt` PyPI package parses only the
subtitle *envelope* (index / timing / cue text), not the DJI GPS fields inside
each cue.

## The envelope

Standard SRT cues separated by blank lines:

```
<index>
<HH:MM:SS,mmm> --> <HH:MM:SS,mmm>
<payload line(s)>
```

One cue per video frame, so a short clip yields thousands of cues. Real exports
sometimes leave stray whitespace on the "blank" separator line — split on
`\r?\n[ \t]*\r?\n`, not a strict empty line, or frames merge.

## Two payload families

DJI's GPS encoding falls into two on-disk families (model / firmware dependent):

1. **bracket** — modern models (Mini 3/4 Pro, Air, Mavic 2/3). Telemetry on one
   line as bracketed key:value tokens; latitude and longitude are **labelled**:
   ```
   <font size="28">FrameCnt: 1, DiffTime: 33ms
   2024-08-26 18:06:42.460
   [iso: 110] [shutter: 1/80.0] ... [latitude: 4.798240] [longitude: -75.691450] [rel_alt: 0.000 abs_alt: 1496.934] [ct: 6256] </font>
   ```
   The spaced-colon variant `[latitude : X] [longitude : Y]` (older Mavic/Air)
   is the same family. **lat/lon always sit on one physical line** — a parser
   should require that, so a malformed/merged cue can't pair one frame's latitude
   with another frame's longitude.

2. **paren** — older DJI GO OSD (Phantom, early Mavic). Positional, not labelled:
   ```
   HOME(-122.419400,37.774900) 2020.04.15 10:30:00
   GPS(-122.419400,37.774900,16) BAROMETER:8.50
   ```
   **Gotcha: the order is `GPS(longitude, latitude, altitude)` — longitude
   first.** Easy to transpose; pin the field semantics explicitly.

## Two real-world gotchas

- **Null-island pre-lock frames.** Before GPS lock DJI emits `latitude: 0.000000
  longitude: 0.000000`. These are not a real fix — skip `(0,0)` and return the
  first frame with a range-valid, non-null coordinate.
- **Partial vs absent.** Distinguish "GPS tokens present but never a valid fix"
  (flag it — e.g. all frames null-island) from "no GPS tokens at all". Emitting a
  guessed coordinate for the former is worse than admitting failure.

## How geosorter uses it

`src/geosorter/srt_parser.py::parse_srt` runs ordered regex probes (`bracket`,
then `paren`) against each cue, returns the first valid fix, and flags
`gps_source='srt_partial'` when tokens exist but no fix is range-valid (lat in
[-90,90], lon in [-180,180], not `(0,0)`). `src/geosorter/metadata.py` consults
the sibling `.SRT` only for videos lacking embedded EXIF GPS, and retains both
`exif_gps` and `srt_gps` for audit. The index DB's `files.gps_source` column
records provenance (`exif` | `srt` | `srt_partial` | `none`).

## Full flight tracks and playback synchronization

The same parser now exposes the complete usable route:

- `parse_srt_track` returns every valid fix in frame order.
- `parse_srt_track_samples` also retains each cue's subtitle-clock time.
- The payload family is pinned by the first matching cue, so a malformed file that
  mixes bracket and paren syntax cannot interleave differently ordered coordinates.
- Missing or unreadable sidecars return an empty list rather than failing media
  playback.

`GET /api/track/{file_id}` finds the indexed SRT companion and returns:

```json
{
  "points": [[-105.1, 39.7]],
  "samples": [{"time_s": 0.0, "lon": -105.1, "lat": 39.7}]
}
```

Both arrays use GeoJSON longitude-first order and are independently downsampled to
at most 500 entries while retaining the first and final fix. `/api/library` sets
`has_track=true` for videos with an SRT companion so the viewer only offers the
flight-path control when a route may exist.

In the browser, selecting the flight path draws a cased line and takeoff marker,
moves the video into a draggable picture-in-map player, and synchronizes a moving
drone marker to `video.currentTime`. Follow mode keeps the map centered on that
marker. A sidecar without enough timestamped samples still draws the static route
but reports that timeline synchronization is unavailable.
