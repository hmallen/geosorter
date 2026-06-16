// Pure helpers for the location-filter panel: aggregate the loaded library features
// into a list of distinct places (each with a count + a covering bounding box) and
// filter that list by a text query. Derived entirely client-side from the already-
// loaded /api/library features — no extra API call.

import type { BBox } from './clusters'
import type { LibraryFeature } from './types'

export interface Place {
  place_string: string
  count: number
  bbox: BBox // [minLon, minLat, maxLon, maxLat] covering every capture in this place
}

// Group features by place_string (skipping null), accumulating a count and the
// bounding box of every capture sharing the place; sorted alphabetically by name.
export function buildPlaces(features: LibraryFeature[]): Place[] {
  const byPlace = new Map<string, Place>()
  for (const f of features) {
    const place = f.properties.place_string
    if (!place) continue
    const [lon, lat] = f.geometry.coordinates
    const existing = byPlace.get(place)
    if (existing) {
      existing.count += 1
      existing.bbox = [
        Math.min(existing.bbox[0], lon),
        Math.min(existing.bbox[1], lat),
        Math.max(existing.bbox[2], lon),
        Math.max(existing.bbox[3], lat),
      ]
    } else {
      byPlace.set(place, { place_string: place, count: 1, bbox: [lon, lat, lon, lat] })
    }
  }
  return [...byPlace.values()].sort((a, b) =>
    a.place_string.localeCompare(b.place_string),
  )
}

// Case-insensitive substring filter over the place name; a blank query keeps all.
export function filterPlaces(places: Place[], query: string): Place[] {
  const q = query.trim().toLowerCase()
  if (!q) return places
  return places.filter((p) => p.place_string.toLowerCase().includes(q))
}
