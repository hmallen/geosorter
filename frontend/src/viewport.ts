// Pure viewport filter: given the in-memory library features and the current map
// bounds, return the subset whose marker falls inside the viewport. Kept DOM-free
// and side-effect-free (mirrors clusters.ts / gridWindow.ts) so the side panel can
// reuse the already-loaded features with no network refetch on pan.
import type { BBox } from './clusters'
import type { LibraryFeature } from './types'

// Normalize a longitude into [-180, 180). MapLibre getBounds() can report west/east
// OUTSIDE that range when the map renders repeated world copies (panning east/west
// past the antimeridian), e.g. west=190 east=210. Feature coordinates are always
// normalized, so the bounds must be normalized too — otherwise the raw comparison
// would match nothing in every world copy but the primary one. Exported: urlState's
// formatHash reuses it to keep a world-copy camera longitude hash-parseable.
export function normalizeLon(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180
}

export function featuresInBounds(features: LibraryFeature[], bounds: BBox): LibraryFeature[] {
  const [west, south, east, north] = bounds
  // A raw span of a full turn or more means every longitude is on screen (zoomed
  // out far enough to show the whole world, possibly with world-copy padding) —
  // keep everything that passes the latitude bounds.
  const allLon = east - west >= 360
  const w = normalizeLon(west)
  const e = normalizeLon(east)
  // After normalization, w > e means the visible band wraps the antimeridian, so the
  // kept longitude band is the "long way" around (lon >= w OR lon <= e) rather than a
  // simple range. Latitude never wraps. Bounds are inclusive on every edge.
  const wrapped = w > e
  return features.filter((f) => {
    const [lon, lat] = f.geometry.coordinates
    if (lat < south || lat > north) return false
    if (allLon) return true
    return wrapped ? lon >= w || lon <= e : lon >= w && lon <= e
  })
}
