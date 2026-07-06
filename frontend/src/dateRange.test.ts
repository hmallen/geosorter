import { describe, it, expect } from 'vitest'
import {
  barHeightPct,
  buildTimeline,
  featureDate,
  filterByDateRange,
  formatRangeLabel,
  handleLeftPct,
  indexFromPointer,
  rangeFromMonths,
  selectionFromRange,
  type MonthBin,
} from './dateRange'
import type { FeatureProps, LibraryFeature } from './types'

// Minimal feature factory: only the date fields matter here; the id keeps
// results identifiable (same pattern as viewport.test.ts).
function feat(
  localDate: string | null,
  captureTs: string | null = null,
  id = localDate ?? 'undated',
): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: { id, local_date: localDate, capture_ts_local: captureTs } as unknown as FeatureProps,
  }
}

const ids = (fs: LibraryFeature[]) => fs.map((f) => f.properties.id)

describe('featureDate', () => {
  it('prefers local_date, falling back to the capture_ts_local date part', () => {
    expect(featureDate({ local_date: '2024-07-04', capture_ts_local: null })).toBe('2024-07-04')
    expect(featureDate({ local_date: null, capture_ts_local: '2024-07-05T14:34:22-06:00' }))
      .toBe('2024-07-05')
    expect(featureDate({ local_date: '2024-07-04', capture_ts_local: '2024-08-01T00:00:00Z' }))
      .toBe('2024-07-04')
  })

  it('returns null for dateless or malformed values', () => {
    expect(featureDate({ local_date: null, capture_ts_local: null })).toBeNull()
    expect(featureDate({ local_date: 'not a date', capture_ts_local: null })).toBeNull()
    expect(featureDate({ local_date: '2024-13-01', capture_ts_local: null })).toBeNull()
  })
})

describe('buildTimeline', () => {
  it('returns [] when no feature has a usable date', () => {
    expect(buildTimeline([])).toEqual([])
    expect(buildTimeline([feat(null)])).toEqual([])
  })

  it('bins per month, densely from min to max with zero-count gap months', () => {
    const fs = [
      feat('2024-11-05', null, 'a'),
      feat('2024-11-20', null, 'b'),
      feat('2025-02-01', null, 'c'),
      feat(null, '2025-02-10T08:00:00-07:00', 'd'), // via capture_ts fallback
      feat(null, null, 'undated'), // skipped
    ]
    expect(buildTimeline(fs)).toEqual([
      { key: '2024-11', count: 2 },
      { key: '2024-12', count: 0 },
      { key: '2025-01', count: 0 },
      { key: '2025-02', count: 2 },
    ])
  })

  it('rolls the year over at December (string month math)', () => {
    const fs = [feat('2023-12-31'), feat('2024-01-01')]
    expect(buildTimeline(fs).map((b) => b.key)).toEqual(['2023-12', '2024-01'])
  })

  it('caps the axis so one corrupt far date cannot explode the bins', () => {
    const fs = [feat('0001-01-01'), feat('2024-01-01')]
    expect(buildTimeline(fs).length).toBeLessThanOrEqual(3600)
  })
})

describe('filterByDateRange', () => {
  const fs = [
    feat('2024-06-30', null, 'before'),
    feat('2024-07-01', null, 'on-from'),
    feat('2024-07-15', null, 'inside'),
    feat('2024-07-31', null, 'on-to'),
    feat('2024-08-01', null, 'after'),
    feat(null, null, 'undated'),
  ]

  it('passes everything through (identity) when the range is null', () => {
    expect(filterByDateRange(fs, null)).toBe(fs)
  })

  it('is inclusive on both bounds and excludes dateless features', () => {
    expect(ids(filterByDateRange(fs, { from: '2024-07-01', to: '2024-07-31' })))
      .toEqual(['on-from', 'inside', 'on-to'])
  })

  it('uses the capture_ts_local fallback date', () => {
    const f = feat(null, '2024-07-10T12:00:00-06:00', 'ts')
    expect(ids(filterByDateRange([f], { from: '2024-07-01', to: '2024-07-31' }))).toEqual(['ts'])
  })
})

const months: MonthBin[] = [
  { key: '2024-11', count: 2 },
  { key: '2024-12', count: 0 },
  { key: '2025-01', count: 5 },
  { key: '2025-02', count: 1 },
]

describe('rangeFromMonths', () => {
  it('maps indices to whole-month bounds (day 01..31)', () => {
    expect(rangeFromMonths(months, 1, 2)).toEqual({ from: '2024-12-01', to: '2025-01-31' })
  })

  it('accepts swapped indices and clamps out-of-range ones', () => {
    expect(rangeFromMonths(months, 2, 1)).toEqual({ from: '2024-12-01', to: '2025-01-31' })
    expect(rangeFromMonths(months, -5, 99)).toEqual({ from: '2024-11-01', to: '2025-02-31' })
  })

  it('returns null for an empty axis', () => {
    expect(rangeFromMonths([], 0, 0)).toBeNull()
  })

  it('roundtrips through selectionFromRange', () => {
    const r = rangeFromMonths(months, 1, 2)
    expect(selectionFromRange(months, r)).toEqual({ start: 1, end: 2 })
  })
})

describe('selectionFromRange', () => {
  it('reads a null range as the full span', () => {
    expect(selectionFromRange(months, null)).toEqual({ start: 0, end: 3 })
  })

  it('clamps a partially-overlapping range to the covered months', () => {
    expect(selectionFromRange(months, { from: '2020-01-01', to: '2024-12-31' }))
      .toEqual({ start: 0, end: 1 })
    expect(selectionFromRange(months, { from: '2025-01-15', to: '2030-01-01' }))
      .toEqual({ start: 2, end: 3 })
  })

  it('degenerates to an edge month for a range entirely outside the axis', () => {
    expect(selectionFromRange(months, { from: '2030-01-01', to: '2030-06-30' }))
      .toEqual({ start: 3, end: 3 })
    expect(selectionFromRange(months, { from: '2010-01-01', to: '2010-06-30' }))
      .toEqual({ start: 0, end: 0 })
  })
})

describe('formatRangeLabel', () => {
  it('names both ends with short month names (no Date parsing)', () => {
    expect(formatRangeLabel({ from: '2025-01-01', to: '2025-06-31' })).toBe('Jan 2025 – Jun 2025')
  })

  it('collapses a single-month range to one label', () => {
    expect(formatRangeLabel({ from: '2025-03-01', to: '2025-03-31' })).toBe('Mar 2025')
  })
})

describe('indexFromPointer', () => {
  it('maps an x offset to the month owning that slice of the width', () => {
    expect(indexFromPointer(0, 400, 4)).toBe(0)
    expect(indexFromPointer(150, 400, 4)).toBe(1)
    expect(indexFromPointer(399, 400, 4)).toBe(3)
  })

  it('clamps outside the strip and survives degenerate geometry', () => {
    expect(indexFromPointer(-50, 400, 4)).toBe(0)
    expect(indexFromPointer(900, 400, 4)).toBe(3)
    expect(indexFromPointer(10, 0, 4)).toBe(0)
    expect(indexFromPointer(10, 400, 0)).toBe(0)
  })
})

describe('barHeightPct', () => {
  it('is proportional to the tallest month with a visible floor', () => {
    expect(barHeightPct(5, 5)).toBe(100)
    expect(barHeightPct(1, 4)).toBe(25)
    expect(barHeightPct(1, 100)).toBe(8) // floor: sparse months stay visible
    expect(barHeightPct(0, 5)).toBe(0)
    expect(barHeightPct(0, 0)).toBe(0)
  })
})

describe('handleLeftPct', () => {
  it('puts the start handle on its month left edge and the end handle on the right edge', () => {
    expect(handleLeftPct({ start: 0, end: 3 }, 4)).toEqual({ start: 0, end: 100 })
    expect(handleLeftPct({ start: 1, end: 1 }, 4)).toEqual({ start: 25, end: 50 })
  })
})
