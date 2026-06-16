import { describe, it, expect } from 'vitest'
import { buildPlaces, filterPlaces } from './locationFilter'
import type { LibraryFeature } from './types'

function feat(id: number, place: string | null, lon: number, lat: number): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      id,
      filename: `f${id}.JPG`,
      place_string: place,
      local_date: null,
      capture_ts_local: null,
      media_type: 'photo',
      codec: null,
      gps_source: 'exif',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      path: `f${id}.JPG`,
    },
  }
}

describe('buildPlaces', () => {
  it('groups features by place_string with a count and a covering bbox', () => {
    const places = buildPlaces([
      feat(1, 'Moab, Utah, United States', -109.5, 38.5),
      feat(2, 'Moab, Utah, United States', -109.6, 38.7),
      feat(3, 'Denver, Colorado, United States', -105.0, 39.7),
    ])
    expect(places).toHaveLength(2)
    const moab = places.find((p) => p.place_string === 'Moab, Utah, United States')!
    expect(moab.count).toBe(2)
    // bbox is [minLon, minLat, maxLon, maxLat] over the two Moab captures
    expect(moab.bbox).toEqual([-109.6, 38.5, -109.5, 38.7])
  })

  it('skips features with a null place_string', () => {
    const places = buildPlaces([feat(1, null, 0, 0), feat(2, 'X, Y, Z', 1, 1)])
    expect(places.map((p) => p.place_string)).toEqual(['X, Y, Z'])
  })

  it('sorts places alphabetically by place_string', () => {
    const places = buildPlaces([
      feat(1, 'Zion, Utah, United States', 0, 0),
      feat(2, 'Aspen, Colorado, United States', 1, 1),
    ])
    expect(places.map((p) => p.place_string)).toEqual([
      'Aspen, Colorado, United States',
      'Zion, Utah, United States',
    ])
  })

  it('makes a degenerate bbox for a single-capture place', () => {
    const [p] = buildPlaces([feat(1, 'Solo, X, Y', -100, 40)])
    expect(p.bbox).toEqual([-100, 40, -100, 40])
  })
})

describe('filterPlaces', () => {
  const places = buildPlaces([
    feat(1, 'Moab, Utah, United States', 0, 0),
    feat(2, 'Denver, Colorado, United States', 1, 1),
    feat(3, 'Boulder, Colorado, United States', 2, 2),
  ])

  it('case-insensitively matches a substring of the place string', () => {
    expect(filterPlaces(places, 'colorado').map((p) => p.place_string)).toEqual([
      'Boulder, Colorado, United States',
      'Denver, Colorado, United States',
    ])
  })

  it('returns every place for a blank query', () => {
    expect(filterPlaces(places, '   ')).toHaveLength(3)
  })

  it('returns nothing when no place matches', () => {
    expect(filterPlaces(places, 'antarctica')).toEqual([])
  })
})
