import { describe, it, expect } from 'vitest'
import { featuresInBounds } from './viewport'
import type { BBox } from './clusters'
import type { FeatureProps, LibraryFeature } from './types'

// Minimal feature factory: only the geometry matters for the bounds filter; the
// id keeps results identifiable. properties is cast since the filter never reads it.
function feat(lon: number, lat: number, id = `${lon},${lat}`): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: { id } as unknown as FeatureProps,
  }
}

const ids = (fs: LibraryFeature[]) => fs.map((f) => f.properties.id)

describe('featuresInBounds', () => {
  // A simple, un-wrapped box over the US-ish region: [west, south, east, north].
  const box: BBox = [-100, 30, -90, 40]

  it('keeps a feature inside the bounds', () => {
    const fs = [feat(-95, 35, 'in')]
    expect(ids(featuresInBounds(fs, box))).toEqual(['in'])
  })

  it('drops features outside each of the four edges', () => {
    const fs = [
      feat(-105, 35, 'west'), // lon < west
      feat(-85, 35, 'east'), // lon > east
      feat(-95, 25, 'south'), // lat < south
      feat(-95, 45, 'north'), // lat > north
      feat(-95, 35, 'in'),
    ]
    expect(ids(featuresInBounds(fs, box))).toEqual(['in'])
  })

  it('treats the bounds as inclusive (feature exactly on an edge / corner is kept)', () => {
    const fs = [
      feat(-100, 30, 'sw-corner'),
      feat(-90, 40, 'ne-corner'),
      feat(-95, 30, 'south-edge'),
      feat(-100, 35, 'west-edge'),
    ]
    expect(ids(featuresInBounds(fs, box)).sort()).toEqual(
      ['ne-corner', 'south-edge', 'sw-corner', 'west-edge'].sort(),
    )
  })

  it('returns an empty array for empty input', () => {
    expect(featuresInBounds([], box)).toEqual([])
  })

  it('preserves input order and identity of the kept features', () => {
    const a = feat(-95, 35, 'a')
    const b = feat(-105, 35, 'out')
    const c = feat(-92, 38, 'c')
    const result = featuresInBounds([a, b, c], box)
    expect(result).toEqual([a, c])
    expect(result[0]).toBe(a) // same reference, not a copy
  })

  describe('antimeridian-wrapped bounds (west > east)', () => {
    // A viewport crossing the 180° meridian: covers the Pacific from +170 east
    // through 180 to -170. MapLibre getBounds() returns west=170, east=-170.
    const wrapped: BBox = [170, -10, -170, 10]

    it('keeps features on both sides of the antimeridian', () => {
      const fs = [
        feat(175, 0, 'east-of-170'),
        feat(-175, 0, 'west-of-minus170'),
        feat(180, 0, 'on-180'),
      ]
      expect(ids(featuresInBounds(fs, wrapped)).sort()).toEqual(
        ['east-of-170', 'on-180', 'west-of-minus170'].sort(),
      )
    })

    it('drops a feature that falls in the un-covered longitude gap', () => {
      // 0° lon is between -170 and 170 the "short way" — NOT in the wrapped box.
      const fs = [feat(0, 0, 'gap'), feat(160, 0, 'gap2')]
      expect(featuresInBounds(fs, wrapped)).toEqual([])
    })

    it('still applies the latitude bounds inside a wrapped box', () => {
      const fs = [feat(175, 20, 'too-north'), feat(175, 5, 'ok')]
      expect(ids(featuresInBounds(fs, wrapped))).toEqual(['ok'])
    })
  })

  describe('world-copy bounds (longitudes outside [-180,180])', () => {
    it('matches features when panned into the eastern world copy (west/east > 180)', () => {
      // MapLibre can report west=190 east=210 after panning east past 180°; those
      // normalize to [-170, -150], so a real feature at lon -160 must still match.
      const bounds: BBox = [190, -10, 210, 10]
      const fs = [feat(-160, 0, 'in'), feat(0, 0, 'out')]
      expect(ids(featuresInBounds(fs, bounds))).toEqual(['in'])
    })

    it('matches features when panned into the western world copy (west/east < -180)', () => {
      // west=-210 east=-190 normalize to [150, 170].
      const bounds: BBox = [-210, -10, -190, 10]
      const fs = [feat(160, 0, 'in'), feat(0, 0, 'out')]
      expect(ids(featuresInBounds(fs, bounds))).toEqual(['in'])
    })

    it('keeps every feature when the longitude span covers a full turn or more', () => {
      // Zoomed all the way out: west=-200 east=200 (span 400° >= 360°) → whole world.
      const bounds: BBox = [-200, -85, 200, 85]
      const fs = [feat(-175, 0, 'a'), feat(0, 0, 'b'), feat(175, 0, 'c')]
      expect(ids(featuresInBounds(fs, bounds))).toEqual(['a', 'b', 'c'])
    })
  })
})
