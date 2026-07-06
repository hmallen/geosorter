// Pure helpers for the flight-track overlay: bbox + GeoJSON + layer styles for
// a video's GPS path (fetched via api.fetchTrack as [lon, lat] pairs).

import type { LineLayerSpecification } from 'maplibre-gl'
import type { BBox } from './clusters'

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
