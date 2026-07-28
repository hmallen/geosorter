// Pure helpers for the flight-track overlay: bbox + GeoJSON + layer styles for
// a video's GPS path (fetched via api.fetchTrack as [lon, lat] pairs).

import type { LineLayerSpecification } from 'maplibre-gl'
import type { BBox } from './clusters'
import type { AltitudeRef, FlightTrackSample } from './types'

// Dark casing under a brand-teal line: readable on both the dark vector
// basemap and bright satellite imagery (same rationale as the pin ring).
export const TRACK_CASING_LAYER: LineLayerSpecification = {
  id: 'flight-track-casing',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  paint: {
    'line-color': '#0f1116',
    'line-width': 6,
    'line-opacity': 0.7,
  },
}

export const TRACK_LINE_LAYER: LineLayerSpecification = {
  id: 'flight-track-line',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  paint: {
    'line-color': '#5eead4', // --accent-text teal
    'line-width': 3,
  },
}

// Bounding box of a track for MapView's fitBounds. A degenerate track (0 or 1
// points) yields null — there is nothing to fit (MapView's maxZoom would cap a
// single-point bbox anyway, but a 1-fix "track" isn't worth a camera move).
export function trackBBox(points: [number, number][]): BBox | null {
  if (points.length < 2) return null
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  for (const [lon, lat] of points) {
    if (lon < west) west = lon
    if (lon > east) east = lon
    if (lat < south) south = lat
    if (lat > north) north = lat
  }
  return [west, south, east, north]
}

// LineString Feature for the maplibre track Source. Kept a plain object (no
// maplibre types) so it stays test-friendly.
export function trackLine(points: [number, number][]): {
  type: 'Feature'
  geometry: { type: 'LineString'; coordinates: [number, number][] }
  properties: Record<string, never>
} {
  return {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: points },
    properties: {},
  }
}

// The drone's telemetry at one instant of the video clock: where it was, and how
// high. `altitude` is null when the surrounding cues carried no height token —
// a sidecar can have GPS without altitude, and a missing height must not hide
// the position marker.
export interface TrackState {
  position: [number, number]
  altitude: number | null
}

// Interpolate between the two fixes bracketing a value. A null endpoint falls
// back to the other one rather than interpolating toward nothing, so an isolated
// gap in the altitude track holds the last known height instead of blanking.
function lerpOptional(
  left: number | null | undefined,
  right: number | null | undefined,
  ratio: number,
): number | null {
  if (left == null) return right ?? null
  if (right == null) return left
  return left + (right - left) * ratio
}

// Resolve the drone's map position and altitude at a video timestamp. An
// upper-bound binary search intentionally selects the LAST sample at an exact
// duplicate timestamp, then interpolates toward the next later fix.
export function trackStateAtTime(
  samples: FlightTrackSample[],
  timeS: number,
): TrackState | null {
  if (samples.length === 0 || timeS < samples[0].time_s) return null
  const last = samples[samples.length - 1]
  if (timeS >= last.time_s) {
    return { position: [last.lon, last.lat], altitude: last.alt ?? null }
  }

  let low = 0
  let high = samples.length
  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (samples[mid].time_s <= timeS) low = mid + 1
    else high = mid
  }
  const left = samples[Math.max(0, low - 1)]
  const right = samples[low]
  if (!right || right.time_s <= left.time_s) {
    return { position: [left.lon, left.lat], altitude: left.alt ?? null }
  }
  const ratio = (timeS - left.time_s) / (right.time_s - left.time_s)
  return {
    position: [
      left.lon + (right.lon - left.lon) * ratio,
      left.lat + (right.lat - left.lat) * ratio,
    ],
    altitude: lerpOptional(left.alt, right.alt, ratio),
  }
}

// Position-only view of trackStateAtTime, kept for callers (and the map marker)
// that never need the height.
export function positionAtTime(
  samples: FlightTrackSample[],
  timeS: number,
): [number, number] | null {
  return trackStateAtTime(samples, timeS)?.position ?? null
}

// Readout text for the live altitude badge. DJI writes metres; whole metres are
// what a per-frame readout can show without the last digit flickering as noise.
// `-0` is normalized away — a drone at the takeoff plane reads "0 m".
export function formatAltitude(metres: number): string {
  const rounded = Math.round(metres)
  return `${rounded === 0 ? 0 : rounded} m`
}

// Wording for the badge's tooltip/aria label. The datum matters: 120 m above
// takeoff and 120 m above sea level are different flights.
export function altitudeTitle(ref: AltitudeRef | null | undefined): string {
  if (ref === 'relative') return 'Altitude above takeoff'
  if (ref === 'absolute') return 'Altitude above sea level'
  return 'Altitude'
}

export interface PipPosition {
  x: number
  y: number
}

export interface RectSize {
  width: number
  height: number
}

export const PIP_MIN_WIDTH = 280
export const PIP_ASPECT_RATIO = 16 / 9
export const PIP_CHROME_HEIGHT = 50

// Keep a dragged PiP wholly inside the viewport with a small reachable margin.
export function clampPipPosition(
  position: PipPosition,
  pip: RectSize,
  viewport: RectSize,
  margin = 12,
): PipPosition {
  const maxX = Math.max(margin, viewport.width - pip.width - margin)
  const maxY = Math.max(margin, viewport.height - pip.height - margin)
  return {
    x: Math.min(maxX, Math.max(margin, position.x)),
    y: Math.min(maxY, Math.max(margin, position.y)),
  }
}

// Keep a resized PiP inside the viewport while preserving the video's aspect
// ratio. The fixed chrome height accounts for the title bar and resize grip.
export function clampPipWidth(
  width: number,
  position: PipPosition,
  viewport: RectSize,
  margin = 12,
): number {
  const maxByWidth = viewport.width - position.x - margin
  const availableVideoHeight = viewport.height - position.y - margin - PIP_CHROME_HEIGHT
  const maxByHeight = availableVideoHeight * PIP_ASPECT_RATIO
  const maxWidth = Math.max(0, Math.min(maxByWidth, maxByHeight))
  const effectiveMin = Math.min(PIP_MIN_WIDTH, maxWidth)
  return Math.min(maxWidth, Math.max(effectiveMin, width))
}
