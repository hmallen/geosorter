// Pure helpers for the flight-track overlay: bbox + GeoJSON + layer styles for
// a video's GPS path (fetched via api.fetchTrack as [lon, lat] pairs).

import type { LineLayerSpecification } from 'maplibre-gl'
import type { BBox } from './clusters'
import type { FlightTrackSample } from './types'

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

// Resolve the drone's map position at a video timestamp. An upper-bound binary
// search intentionally selects the LAST sample at an exact duplicate timestamp,
// then interpolates toward the next later fix.
export function positionAtTime(
  samples: FlightTrackSample[],
  timeS: number,
): [number, number] | null {
  if (samples.length === 0 || timeS < samples[0].time_s) return null
  const last = samples[samples.length - 1]
  if (timeS >= last.time_s) return [last.lon, last.lat]

  let low = 0
  let high = samples.length
  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (samples[mid].time_s <= timeS) low = mid + 1
    else high = mid
  }
  const left = samples[Math.max(0, low - 1)]
  const right = samples[low]
  if (!right || right.time_s <= left.time_s) return [left.lon, left.lat]
  const ratio = (timeS - left.time_s) / (right.time_s - left.time_s)
  return [
    left.lon + (right.lon - left.lon) * ratio,
    left.lat + (right.lat - left.lat) * ratio,
  ]
}

export interface PipPosition {
  x: number
  y: number
}

export interface RectSize {
  width: number
  height: number
}

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
