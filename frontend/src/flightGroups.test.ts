import { describe, expect, it } from 'vitest'
import {
  buildFlightIndex,
  changeGroupMode,
  groupFlightFeatures,
  initialGroupingFilterState,
  toggleGroupingCategory,
} from './flightGroups'
import type { FeatureProps, LibraryFeature } from './types'

function feat(
  id: number,
  capture_ts_local: string | null,
  duration_s: number | null,
  over: Partial<FeatureProps> = {},
): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: {
      id,
      filename: `DJI_${String(id).padStart(4, '0')}.MP4`,
      place_string: null,
      local_date: capture_ts_local?.slice(0, 10) ?? null,
      capture_ts_local,
      media_type: 'video',
      codec: 'h265',
      duration_s,
      gps_source: 'srt',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      path: `clips/${id}.mp4`,
      ...over,
    },
  }
}

function keys(index: ReturnType<typeof buildFlightIndex>, ids: number[]): string[] {
  return ids.map((id) => index.get(id)?.key ?? '')
}

describe('buildFlightIndex', () => {
  it('merges overlaps, exact adjacency, and the inclusive two-second tolerance', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:09-06:00', 10),
      feat(3, '2026-08-28T10:00:19-06:00', 10),
      feat(4, '2026-08-28T10:00:31-06:00', 10),
      feat(5, '2026-08-28T10:00:43.001-06:00', 10),
    ]
    const index = buildFlightIndex(files)
    expect(new Set(keys(index, [1, 2, 3, 4]))).toEqual(new Set(['flight:1']))
    expect(index.get(5)?.key).toBe('flight:5')
  })

  it('uses the rolling maximum end across overlapping clips', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 100),
      feat(2, '2026-08-28T10:00:50-06:00', 10),
      feat(3, '2026-08-28T10:01:41.500-06:00', 5),
    ]
    const index = buildFlightIndex(files)
    expect(new Set(keys(index, [1, 2, 3]))).toEqual(new Set(['flight:1']))
  })

  it('sorts absolute instants across offsets and breaks timestamp ties by id', () => {
    const files = [
      feat(20, '2026-08-28T10:00:00-06:00', 1), // 16:00Z
      feat(30, '2026-08-28T09:00:00-05:00', 1), // 14:00Z
      feat(10, '2026-08-28T10:00:00-06:00', 1),
    ]
    const groups = groupFlightFeatures(files, buildFlightIndex(files), 'asc')
    expect(groups.map((g) => g.files.map((f) => f.properties.id))).toEqual([[30], [10, 20]])
  })

  it('excludes photos and hyperlapses', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:01-06:00', 10, { media_type: 'photo' }),
      feat(3, '2026-08-28T10:00:02-06:00', 10, { capture_kind: 'hyperlapse' }),
    ]
    const index = buildFlightIndex(files)
    expect([...index.keys()]).toEqual([1])
  })

  it('makes every missing or invalid interval a separate singleton', () => {
    const files = [
      feat(1, null, 10),
      feat(2, 'not-a-time', 10),
      feat(3, '2026-08-28T10:00:00-06:00', null),
      feat(4, '2026-08-28T10:00:00-06:00', 0),
      feat(5, '2026-08-28T10:00:00-06:00', Number.NaN),
    ]
    const index = buildFlightIndex(files)
    expect(new Set(keys(index, [1, 2, 3, 4, 5])).size).toBe(5)
    expect(index.get(1)?.label).toContain('Flight time unavailable')
    expect(index.get(3)?.label).toContain('duration unavailable')
  })

  it('accepts microsecond timestamps without shifting their local labels', () => {
    const [group] = groupFlightFeatures(
      [feat(1, '2026-08-28T14:10:00.123456+09:00', 60)],
      buildFlightIndex([feat(1, '2026-08-28T14:10:00.123456+09:00', 60)]),
      'asc',
    )
    expect(group.label).toBe('Flight · August 28, 2026 · 2:10–2:11 PM')
  })
})

describe('groupFlightFeatures', () => {
  it('keeps clips chronological while reversing only the flight order', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T11:00:00-06:00', 10),
      feat(4, '2026-08-28T11:00:11-06:00', 10),
    ]
    const index = buildFlightIndex(files)
    const desc = groupFlightFeatures([files[3], files[0], files[2], files[1]], index, 'desc')
    expect(desc.map((g) => g.files.map((f) => f.properties.id))).toEqual([[3, 4], [1, 2]])
    const asc = groupFlightFeatures(files, index, 'asc')
    expect(asc.map((g) => g.files.map((f) => f.properties.id))).toEqual([[1, 2], [3, 4]])
  })

  it('keeps one assignment when the viewport omits an intermediate member', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T10:00:22-06:00', 10),
    ]
    const groups = groupFlightFeatures([files[0], files[2]], buildFlightIndex(files), 'desc')
    expect(groups).toHaveLength(1)
    expect(groups[0].files.map((f) => f.properties.id)).toEqual([1, 3])
  })

  it('formats same-day and cross-midnight ranges in capture-local wall time', () => {
    const same = [
      feat(1, '2026-08-28T14:10:00-06:00', 60),
      feat(2, '2026-08-28T14:11:01-06:00', 959),
    ]
    expect(groupFlightFeatures(same, buildFlightIndex(same), 'asc')[0].label)
      .toBe('Flight · August 28, 2026 · 2:10–2:27 PM')

    const overnight = [feat(3, '2026-08-28T23:59:00-06:00', 660)]
    expect(groupFlightFeatures(overnight, buildFlightIndex(overnight), 'asc')[0].label)
      .toBe('Flight · August 28, 2026, 11:59 PM – August 29, 2026, 12:10 AM')
  })
})

describe('flight grouping filter state', () => {
  it('locks Videos only and restores the exact prior categories on exit', () => {
    let state = initialGroupingFilterState()
    state = toggleGroupingCategory(state, 'photo')
    state = toggleGroupingCategory(state, 'hyperlapse')
    expect([...state.enabled]).toEqual(['video', 'panorama'])

    state = changeGroupMode(state, 'flight')
    expect([...state.enabled]).toEqual(['video'])
    expect(toggleGroupingCategory(state, 'photo')).toBe(state)

    state = changeGroupMode(state, 'day')
    expect([...state.enabled]).toEqual(['video', 'panorama'])
    expect(state.beforeFlight).toBeNull()
  })
})
