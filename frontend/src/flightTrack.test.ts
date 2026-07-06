import { describe, it, expect } from 'vitest'
import { trackBBox, trackLine } from './flightTrack'

describe('trackBBox', () => {
  it('returns the [west, south, east, north] envelope of the points', () => {
    const bbox = trackBBox([
      [-105.3, 40.0],
      [-105.1, 40.2],
      [-105.2, 39.9],
    ])
    expect(bbox).toEqual([-105.3, 39.9, -105.1, 40.2])
  })

  it('is null for a degenerate track (nothing to fit)', () => {
    expect(trackBBox([])).toBeNull()
    expect(trackBBox([[-105.3, 40.0]])).toBeNull()
  })
})

describe('trackLine', () => {
  it('wraps the points as a GeoJSON LineString feature', () => {
    const pts: [number, number][] = [
      [-105.3, 40.0],
      [-105.1, 40.2],
    ]
    expect(trackLine(pts)).toEqual({
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: pts },
      properties: {},
    })
  })
})
