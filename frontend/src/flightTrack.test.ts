import { describe, it, expect } from 'vitest'
import {
  TRACK_CASING_LAYER,
  TRACK_INACTIVE_CASING_LAYER,
  TRACK_INACTIVE_LINE_LAYER,
  TRACK_LINE_LAYER,
  altitudeTitle,
  clampPipPosition,
  clampPipWidth,
  formatAltitude,
  loadAvailableTracks,
  nearestTrackTime,
  positionAtTime,
  trackBBox,
  trackCollection,
  trackLine,
  trackStateAtTime,
  tracksBBox,
} from './flightTrack'
import type { FlightTrack, LibraryFeature } from './types'

function video(id: number, hasTrack = true): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: {
      id,
      filename: `DJI_${id}.MP4`,
      place_string: null,
      local_date: '2026-08-28',
      capture_ts_local: `2026-08-28T10:00:${String(id).padStart(2, '0')}-06:00`,
      media_type: 'video',
      codec: 'h265',
      duration_s: 1,
      gps_source: 'srt',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      has_track: hasTrack,
      path: `clips/${id}.mp4`,
    },
  }
}

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

describe('multi-track geometry', () => {
  const tracks = [
    {
      fileId: 1,
      filename: 'one.mp4',
      points: [[-105, 40], [-104, 41]] as [number, number][],
      samples: [],
      altitudeRef: null,
    },
    {
      fileId: 2,
      filename: 'two.mp4',
      points: [[-103, 39], [-102, 42]] as [number, number][],
      samples: [],
      altitudeRef: null,
    },
  ]

  it('marks only the current video active while keeping every path present', () => {
    const collection = trackCollection(tracks, 2)
    expect(collection.features).toHaveLength(2)
    expect(collection.features.map((feature) => feature.properties.active))
      .toEqual([false, true])
  })

  it('fits the envelope of all loaded flight paths', () => {
    expect(tracksBBox(tracks)).toEqual([-105, 39, -102, 42])
  })
})

describe('multi-track layer visibility', () => {
  it('keeps inactive routes strongly cased and visually distinct from the active route', () => {
    expect(TRACK_INACTIVE_CASING_LAYER.paint?.['line-width']).toBe(7)
    expect(TRACK_INACTIVE_CASING_LAYER.paint?.['line-opacity']).toBeGreaterThanOrEqual(0.8)
    expect(TRACK_INACTIVE_LINE_LAYER.paint?.['line-width']).toBe(3.5)
    expect(TRACK_INACTIVE_LINE_LAYER.paint?.['line-opacity']).toBeGreaterThanOrEqual(0.9)
    expect(TRACK_INACTIVE_LINE_LAYER.paint?.['line-dasharray']).toEqual([1.5, 1.1])
    expect(TRACK_CASING_LAYER.paint?.['line-width']).toBeGreaterThan(
      TRACK_INACTIVE_CASING_LAYER.paint?.['line-width'] as number,
    )
    expect(TRACK_LINE_LAYER.paint?.['line-width']).toBeGreaterThan(
      TRACK_INACTIVE_LINE_LAYER.paint?.['line-width'] as number,
    )
  })
})

describe('loadAvailableTracks', () => {
  it('keeps chronological input order, skips unavailable members, and bounds concurrency', async () => {
    const files = [video(1), video(2), video(3), video(4, false)]
    let active = 0
    let maxActive = 0
    const fetcher = async (id: number): Promise<FlightTrack> => {
      active += 1
      maxActive = Math.max(maxActive, active)
      try {
        await new Promise((resolve) => setTimeout(resolve, id === 1 ? 8 : 2))
        if (id === 2) throw new Error('missing')
        return {
          points: [[id, id], [id + 0.5, id + 0.5]],
          samples: [],
          altitudeRef: null,
        }
      } finally {
        active -= 1
      }
    }

    const result = await loadAvailableTracks(files, fetcher, 2)
    expect(maxActive).toBeLessThanOrEqual(2)
    expect(result.tracks.map((track) => track.fileId)).toEqual([1, 3])
    expect(result.totalCount).toBe(4)
    expect(result.unavailableCount).toBe(2)
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

describe('trackStateAtTime', () => {
  const samples = [
    { time_s: 1, lon: -105, lat: 40, alt: 100 },
    { time_s: 3, lon: -103, lat: 42, alt: 140 },
    { time_s: 5, lon: -101, lat: 44, alt: 120 },
  ]

  it('interpolates altitude alongside the position', () => {
    expect(trackStateAtTime(samples, 2)).toEqual({ position: [-104, 41], altitude: 120 })
    expect(trackStateAtTime(samples, 3)).toEqual({ position: [-103, 42], altitude: 140 })
  })

  it('hides before GPS lock and holds the final altitude after the last fix', () => {
    expect(trackStateAtTime(samples, 0.99)).toBeNull()
    expect(trackStateAtTime(samples, 8)).toEqual({ position: [-101, 44], altitude: 120 })
  })

  it('reports a null altitude for fixes with no height token', () => {
    const noAlt = [
      { time_s: 1, lon: -105, lat: 40 },
      { time_s: 3, lon: -103, lat: 42 },
    ]
    expect(trackStateAtTime(noAlt, 2)).toEqual({ position: [-104, 41], altitude: null })
  })

  it('holds the known height across a one-sided gap instead of blanking', () => {
    const gap = [
      { time_s: 1, lon: -105, lat: 40, alt: 90 },
      { time_s: 3, lon: -103, lat: 42, alt: null },
      { time_s: 5, lon: -101, lat: 44, alt: 110 },
    ]
    expect(trackStateAtTime(gap, 2)?.altitude).toBe(90)
    expect(trackStateAtTime(gap, 4)?.altitude).toBe(110)
  })
})

describe('nearestTrackTime', () => {
  const straight = [
    { x: 0, y: 0, timeS: 10 },
    { x: 10, y: 0, timeS: 20 },
  ]

  it('interpolates along the nearest projected segment and clamps to its endpoints', () => {
    expect(nearestTrackTime(straight, { x: 4, y: 3 }, 10)).toBe(14)
    expect(nearestTrackTime(straight, { x: -5, y: 0 }, 10)).toBe(10)
    expect(nearestTrackTime(straight, { x: 15, y: 0 }, 10)).toBe(20)
  })

  it('handles empty and single-sample telemetry', () => {
    expect(nearestTrackTime([], { x: 0, y: 0 }, 0)).toBeNull()
    expect(nearestTrackTime([{ x: 4, y: 5, timeS: 12 }], { x: 99, y: 99 }, 0)).toBe(12)
  })

  it('uses the current time to disambiguate a self-crossing route', () => {
    const crossing = [
      { x: -10, y: -10, timeS: 0 },
      { x: 10, y: 10, timeS: 10 },
      { x: -10, y: 10, timeS: 20 },
      { x: 10, y: -10, timeS: 30 },
    ]
    expect(nearestTrackTime(crossing, { x: 0, y: 0 }, 4)).toBe(5)
    expect(nearestTrackTime(crossing, { x: 0, y: 0 }, 24)).toBe(25)
  })

  it('preserves the nearby timestamp across a stationary segment', () => {
    const stationary = [
      { x: 0, y: 0, timeS: 10 },
      { x: 0, y: 0, timeS: 20 },
      { x: 10, y: 0, timeS: 30 },
    ]
    expect(nearestTrackTime(stationary, { x: 0, y: 0 }, 16)).toBe(16)
    expect(nearestTrackTime(stationary, { x: 0, y: 0 }, 4)).toBe(10)
  })
})

describe('formatAltitude', () => {
  it('rounds to whole metres and normalizes -0', () => {
    expect(formatAltitude(120.4)).toBe('120 m')
    expect(formatAltitude(-12.6)).toBe('-13 m')
    expect(formatAltitude(-0.2)).toBe('0 m')
  })
})

describe('altitudeTitle', () => {
  it('names the datum so the number is never ambiguous', () => {
    expect(altitudeTitle('relative')).toBe('Altitude above takeoff')
    expect(altitudeTitle('absolute')).toBe('Altitude above sea level')
    expect(altitudeTitle(null)).toBe('Altitude')
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
