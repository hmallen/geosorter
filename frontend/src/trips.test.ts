import { describe, it, expect } from 'vitest'
import {
  buildTrips,
  dayOrdinal,
  filterTrips,
  formatTripDates,
  haversineKm,
  DEFAULT_GAP_DAYS,
  GAP_CHOICES,
  TRIP_SPLIT_KM,
} from './trips'
import type { LibraryFeature } from './types'

function feat(
  id: number,
  place: string | null,
  date: string | null,
  lon = 0,
  lat = 0,
  ts: string | null = null,
): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      id,
      filename: `f${id}.JPG`,
      place_string: place,
      local_date: date,
      capture_ts_local: ts,
      media_type: 'photo',
      codec: null,
      duration_s: null,
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

const PEREIRA = 'Pereira, Risaralda, Colombia'
const BOGOTA = 'Bogotá, Bogota D.C., Colombia'
const MOAB = 'Moab, Utah, United States'

describe('dayOrdinal', () => {
  it('anchors the epoch at 1970-01-01', () => {
    expect(dayOrdinal('1970-01-01')).toBe(0)
  })

  it('counts through a leap-year February', () => {
    expect(dayOrdinal('2024-03-01') - dayOrdinal('2024-02-28')).toBe(2)
  })

  it('counts across a year boundary', () => {
    expect(dayOrdinal('2025-01-01') - dayOrdinal('2024-12-31')).toBe(1)
  })
})

describe('formatTripDates', () => {
  it('formats a single day', () => {
    expect(formatTripDates('2024-03-09', '2024-03-09')).toBe('Mar 9, 2024')
  })

  it('formats a same-month range with a tight en dash', () => {
    expect(formatTripDates('2024-03-09', '2024-03-17')).toBe('Mar 9–17, 2024')
  })

  it('formats a cross-month range with a spaced en dash', () => {
    expect(formatTripDates('2024-03-28', '2024-04-02')).toBe('Mar 28 – Apr 2, 2024')
  })

  it('formats a cross-year range with both years', () => {
    expect(formatTripDates('2024-12-28', '2025-01-03')).toBe('Dec 28, 2024 – Jan 3, 2025')
  })
})

describe('buildTrips', () => {
  it('splits a trip where the day gap exceeds maxGapDays, newest trip first', () => {
    const trips = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09'),
        feat(2, PEREIRA, '2024-03-10'),
        feat(3, PEREIRA, '2024-03-11'),
        feat(4, PEREIRA, '2024-03-15'), // gap 4 > 2 → new trip
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trips).toHaveLength(2)
    expect(trips[0].from).toBe('2024-03-15') // newest first
    expect(trips[0].to).toBe('2024-03-15')
    expect(trips[1].from).toBe('2024-03-09')
    expect(trips[1].to).toBe('2024-03-11')
  })

  it('keeps a gap exactly equal to maxGapDays in one trip', () => {
    const days = [feat(1, PEREIRA, '2024-03-09'), feat(2, PEREIRA, '2024-03-11')]
    expect(buildTrips(days, 2)).toHaveLength(1)
    expect(buildTrips(days, 1)).toHaveLength(2)
  })

  it('excludes dateless captures and returns [] when nothing is dated', () => {
    const trips = buildTrips(
      [feat(1, PEREIRA, '2024-03-09'), feat(2, PEREIRA, null)],
      DEFAULT_GAP_DAYS,
    )
    expect(trips).toHaveLength(1)
    expect(trips[0].count).toBe(1)
    expect(buildTrips([feat(1, PEREIRA, null)], DEFAULT_GAP_DAYS)).toEqual([])
    expect(buildTrips([], DEFAULT_GAP_DAYS)).toEqual([])
  })

  it('accumulates count and a covering bbox over the trip members', () => {
    const [trip] = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09', -75.7, 4.8),
        feat(2, PEREIRA, '2024-03-10', -75.9, 4.6),
        feat(3, PEREIRA, '2024-03-10', -75.5, 5.0),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trip.count).toBe(3)
    expect(trip.bbox).toEqual([-75.9, 4.6, -75.5, 5.0])
  })

  it('labels a single-place trip with the bare place string', () => {
    const [trip] = buildTrips([feat(1, MOAB, '2024-05-01')], DEFAULT_GAP_DAYS)
    expect(trip.placeLabel).toBe(MOAB)
    expect(trip.dateLabel).toBe('May 1, 2024')
    expect(trip.key).toBe('2024-05-01_2024-05-01_0')
  })

  it('labels a mixed trip with the dominant place plus a more-places suffix', () => {
    const [twoPlaces] = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09'),
        feat(2, PEREIRA, '2024-03-10'),
        feat(3, BOGOTA, '2024-03-11'),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(twoPlaces.placeLabel).toBe(`${PEREIRA} +1 more place`)

    const [threePlaces] = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09'),
        feat(2, PEREIRA, '2024-03-09'),
        feat(3, BOGOTA, '2024-03-10'),
        feat(4, MOAB, '2024-03-11'),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(threePlaces.placeLabel).toBe(`${PEREIRA} +2 more places`)
  })

  it('labels a trip with no placed captures as Unknown place', () => {
    const [trip] = buildTrips([feat(1, null, '2024-03-09')], DEFAULT_GAP_DAYS)
    expect(trip.placeLabel).toBe('Unknown place')
  })

  // Moab, UT vs Pereira, Colombia — ~4400 km apart.
  const MOAB_LL: [number, number] = [-109.55, 38.57]
  const PEREIRA_LL: [number, number] = [-75.69, 4.81]

  it('splits on a geographic jump even when the day gap is within the window', () => {
    const trips = buildTrips(
      [
        feat(1, MOAB, '2024-03-09', ...MOAB_LL),
        feat(2, MOAB, '2024-03-10', ...MOAB_LL),
        feat(3, PEREIRA, '2024-03-12', ...PEREIRA_LL), // gap 2 ≤ 7, but ~4400 km away
      ],
      7,
    )
    expect(trips).toHaveLength(2)
    expect(trips[0].placeLabel).toBe(PEREIRA)
    expect(trips[1].placeLabel).toBe(MOAB)
  })

  it('keeps captures within TRIP_SPLIT_KM of each other in one trip', () => {
    // ~55 km hops (0.5° of latitude) — under the 100 km split threshold.
    const trips = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09', -75.69, 4.81),
        feat(2, PEREIRA, '2024-03-10', -75.69, 5.31),
        feat(3, PEREIRA, '2024-03-11', -75.69, 5.81),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trips).toHaveLength(1)
    expect(trips[0].count).toBe(3)
  })

  it('gives same-date trips split by distance distinct keys', () => {
    const trips = buildTrips(
      [
        feat(1, MOAB, '2024-03-09', ...MOAB_LL),
        feat(2, PEREIRA, '2024-03-09', ...PEREIRA_LL),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trips).toHaveLength(2)
    expect(trips[0].key).not.toBe(trips[1].key)
  })

  it('orders same-day captures by timestamp before measuring jumps', () => {
    // A→B→C are ~60 km hops (one trip), but in input order A→C→B the first
    // hop would be ~120 km and split — the timestamp sort must prevent that.
    const trips = buildTrips(
      [
        feat(1, PEREIRA, '2024-03-09', 0, 0, '2024-03-09 08:00:00'),
        feat(3, PEREIRA, '2024-03-09', 0, 1.08, '2024-03-09 16:00:00'),
        feat(2, PEREIRA, '2024-03-09', 0, 0.54, '2024-03-09 12:00:00'),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trips).toHaveLength(1)
    expect(trips[0].count).toBe(3)
  })

  it('orders multiple trips newest-first by end date', () => {
    const trips = buildTrips(
      [
        feat(1, MOAB, '2023-07-01'),
        feat(2, PEREIRA, '2024-03-09'),
        feat(3, BOGOTA, '2025-01-15'),
      ],
      DEFAULT_GAP_DAYS,
    )
    expect(trips.map((t) => t.to)).toEqual(['2025-01-15', '2024-03-09', '2023-07-01'])
  })
})

describe('filterTrips', () => {
  const trips = buildTrips(
    [
      feat(1, PEREIRA, '2024-03-09'),
      feat(2, PEREIRA, '2024-03-10'),
      feat(3, MOAB, '2023-07-01'),
    ],
    DEFAULT_GAP_DAYS,
  )

  it('case-insensitively matches a substring of the place label', () => {
    expect(filterTrips(trips, 'pereira').map((t) => t.placeLabel)).toEqual([PEREIRA])
  })

  it('matches a substring of the date label', () => {
    expect(filterTrips(trips, 'jul').map((t) => t.placeLabel)).toEqual([MOAB])
  })

  it('matches a month-year query against the trip endpoints', () => {
    // 'Mar 2024' never appears contiguously in a dateLabel ('Mar 9–10, 2024'),
    // so the filter must also expose '<Mon> <year>' tokens for each endpoint.
    expect(filterTrips(trips, 'mar 2024').map((t) => t.placeLabel)).toEqual([PEREIRA])
    expect(filterTrips(trips, 'Jul 2023').map((t) => t.placeLabel)).toEqual([MOAB])
  })

  it('returns the input array unchanged for a blank query', () => {
    expect(filterTrips(trips, '   ')).toBe(trips)
  })

  it('returns nothing when no trip matches', () => {
    expect(filterTrips(trips, 'antarctica')).toEqual([])
  })
})

describe('gap constants', () => {
  it('offers the default among the choices', () => {
    expect(GAP_CHOICES).toContain(DEFAULT_GAP_DAYS)
    expect(DEFAULT_GAP_DAYS).toBe(2)
  })
})

describe('haversineKm', () => {
  it('returns 0 for identical points', () => {
    expect(haversineKm(-75.69, 4.81, -75.69, 4.81)).toBe(0)
  })

  it('measures one degree of latitude as ~111 km', () => {
    expect(haversineKm(0, 0, 0, 1)).toBeGreaterThan(110)
    expect(haversineKm(0, 0, 0, 1)).toBeLessThan(112)
  })

  it('puts Moab and Pereira far beyond the split threshold', () => {
    expect(haversineKm(-109.55, 38.57, -75.69, 4.81)).toBeGreaterThan(TRIP_SPLIT_KM)
  })
})
