import { describe, it, expect } from 'vitest'
import {
  clampPipPosition,
  clampPipWidth,
  positionAtTime,
  trackBBox,
  trackLine,
} from './flightTrack'

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

describe('positionAtTime', () => {
  const samples = [
    { time_s: 1, lon: -105, lat: 40 },
    { time_s: 3, lon: -103, lat: 42 },
    { time_s: 5, lon: -101, lat: 44 },
  ]

  it('hides before GPS lock, interpolates, and holds the final fix', () => {
    expect(positionAtTime(samples, 0.99)).toBeNull()
    expect(positionAtTime(samples, 2)).toEqual([-104, 41])
    expect(positionAtTime(samples, 8)).toEqual([-101, 44])
  })

  it('returns exact samples while seeking', () => {
    expect(positionAtTime(samples, 1)).toEqual([-105, 40])
    expect(positionAtTime(samples, 3)).toEqual([-103, 42])
  })

  it('uses the last fix at a duplicate timestamp', () => {
    const duplicate = [
      { time_s: 1, lon: 10, lat: 20 },
      { time_s: 1, lon: 11, lat: 21 },
      { time_s: 2, lon: 12, lat: 22 },
    ]
    expect(positionAtTime(duplicate, 1)).toEqual([11, 21])
    expect(positionAtTime(duplicate, 1.5)).toEqual([11.5, 21.5])
  })

  it('returns null for empty telemetry', () => {
    expect(positionAtTime([], 10)).toBeNull()
  })
})

describe('clampPipPosition', () => {
  it('clamps every edge inside the viewport', () => {
    const pip = { width: 320, height: 200 }
    const viewport = { width: 1000, height: 700 }
    expect(clampPipPosition({ x: -50, y: -10 }, pip, viewport)).toEqual({ x: 12, y: 12 })
    expect(clampPipPosition({ x: 900, y: 650 }, pip, viewport)).toEqual({ x: 668, y: 488 })
  })

  it('leaves an in-bounds drag unchanged', () => {
    expect(
      clampPipPosition(
        { x: 200, y: 100 },
        { width: 320, height: 200 },
        { width: 1000, height: 700 },
      ),
    ).toEqual({ x: 200, y: 100 })
  })
})

describe('clampPipWidth', () => {
  it('allows an in-bounds resize and enforces the minimum width', () => {
    const position = { x: 100, y: 100 }
    const viewport = { width: 1200, height: 800 }
    expect(clampPipWidth(500, position, viewport)).toBe(500)
    expect(clampPipWidth(100, position, viewport)).toBe(280)
  })

  it('limits width by both the right and bottom viewport edges', () => {
    expect(
      clampPipWidth(900, { x: 500, y: 100 }, { width: 1000, height: 800 }),
    ).toBe(488)
    expect(
      clampPipWidth(900, { x: 100, y: 500 }, { width: 1200, height: 800 }),
    ).toBeCloseTo(423.111, 3)
  })
})
