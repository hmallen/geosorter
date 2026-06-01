import { describe, it, expect } from 'vitest'
import { buildIndex, type ClusterOrPoint } from './clusters'
import { selectionFor } from './selection'
import type { LibraryFeature } from './types'

function feat(id: number, lon: number, lat: number): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      id, filename: `f${id}.jpg`, place_string: 'P', local_date: '2024-07-04',
      media_type: 'photo', codec: null, gps_source: 'exif', path: `f${id}.jpg`,
    },
  }
}

describe('selectionFor', () => {
  it('returns all files co-located with a clicked single marker', () => {
    const a = feat(1, -105, 40)
    const b = feat(2, -105, 40) // same coordinate as a
    const c = feat(3, 2.35, 48.85)
    const all = [a, b, c]
    const index = buildIndex(all)
    const point = a as unknown as ClusterOrPoint // a leaf point click

    const sel = selectionFor(index, point, all)
    expect(sel.map((f) => f.properties.id).sort()).toEqual([1, 2])
  })
})
