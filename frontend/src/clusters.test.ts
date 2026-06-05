import { describe, it, expect } from 'vitest'
import { buildIndex, clustersFor, type BBox } from './clusters'
import type { LibraryFeature } from './types'

function feat(id: number, lon: number, lat: number): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      id, filename: `f${id}.jpg`, place_string: 'P', local_date: '2024-07-04',
      media_type: 'photo', codec: null, gps_source: 'exif', path: `f${id}.jpg`,
      capture_kind: null, frame_count: null, star_rating: null,
    },
  }
}

const WORLD: BBox = [-180, -85, 180, 85]
// Two points in Boulder, one in Paris.
const feats = [feat(1, -105.0, 40.0), feat(2, -105.001, 40.001), feat(3, 2.35, 48.85)]

describe('clustering', () => {
  it('collapses nearby points into clusters at low zoom', () => {
    const idx = buildIndex(feats)
    expect(clustersFor(idx, WORLD, 0).length).toBeLessThan(feats.length)
  })

  it('returns individual leaves at high zoom over a tight bbox', () => {
    const idx = buildIndex(feats)
    const tight: BBox = [-105.01, 39.99, -104.99, 40.01]
    const hi = clustersFor(idx, tight, 18)
    expect(hi.every((c) => !('cluster' in c.properties))).toBe(true)
    expect(hi.length).toBe(2) // the two Boulder points, unclustered
  })
})
