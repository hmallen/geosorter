import { describe, expect, it } from 'vitest'
import {
  buildFlightCatalog,
  buildFlightIndex,
  buildFlightRowModel,
  changeGranularity,
  flightHeaderRowIndex,
  groupFlightsByDate,
  initialGroupingFilterState,
  selectionForCatalogFlight,
  selectionForFlight,
  setFlightSubgroups,
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

function flights(
  files: LibraryFeature[],
  dir: 'asc' | 'desc' = 'asc',
  viewport: LibraryFeature[] = files,
) {
  return groupFlightsByDate(viewport, buildFlightCatalog(files), 'day', dir)
    .flatMap((dateGroup) => dateGroup.flights)
}

describe('buildFlightCatalog', () => {
  it('merges overlaps, exact adjacency, and the inclusive two-second tolerance', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:09-06:00', 10),
      feat(3, '2026-08-28T10:00:19-06:00', 10),
      feat(4, '2026-08-28T10:00:31-06:00', 10),
      feat(5, '2026-08-28T10:00:43.001-06:00', 10),
    ]
    const catalog = buildFlightCatalog(files)
    expect(new Set(keys(catalog.assignments, [1, 2, 3, 4]))).toEqual(new Set(['flight:1']))
    expect(catalog.assignments.get(5)?.key).toBe('flight:5')
    expect(catalog.groups.get('flight:1')?.members.map((f) => f.properties.id))
      .toEqual([1, 2, 3, 4])
  })

  it('uses the rolling maximum end across overlapping clips', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 100),
      feat(2, '2026-08-28T10:00:50-06:00', 10),
      feat(3, '2026-08-28T10:01:41.500-06:00', 5),
    ]
    expect(new Set(keys(buildFlightIndex(files), [1, 2, 3])))
      .toEqual(new Set(['flight:1']))
  })

  it('sorts absolute instants across offsets and breaks timestamp ties by id', () => {
    const files = [
      feat(20, '2026-08-28T10:00:00-06:00', 1), // 16:00Z
      feat(30, '2026-08-28T09:00:00-05:00', 1), // 14:00Z
      feat(10, '2026-08-28T10:00:00-06:00', 1),
    ]
    expect(flights(files).map((group) => group.members.map((f) => f.properties.id)))
      .toEqual([[30], [10, 20]])
  })

  it('excludes photos and hyperlapses', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:01-06:00', 10, { media_type: 'photo' }),
      feat(3, '2026-08-28T10:00:02-06:00', 10, { capture_kind: 'hyperlapse' }),
    ]
    expect([...buildFlightCatalog(files).assignments.keys()]).toEqual([1])
  })

  it('makes every missing or invalid interval a separate singleton', () => {
    const files = [
      feat(1, null, 10),
      feat(2, 'not-a-time', 10),
      feat(3, '2026-08-28T10:00:00-06:00', null),
      feat(4, '2026-08-28T10:00:00-06:00', 0),
      feat(5, '2026-08-28T10:00:00-06:00', Number.NaN),
    ]
    const catalog = buildFlightCatalog(files)
    expect(new Set(keys(catalog.assignments, [1, 2, 3, 4, 5])).size).toBe(5)
    expect(catalog.assignments.get(1)?.label).toContain('Flight time unavailable')
    expect(catalog.assignments.get(3)?.label).toContain('duration unavailable')
  })

  it('accepts microsecond timestamps without shifting their local labels', () => {
    const file = feat(1, '2026-08-28T14:10:00.123456+09:00', 60)
    expect(buildFlightCatalog([file]).groups.get('flight:1')?.label)
      .toBe('Flight · August 28, 2026 · 2:10–2:11 PM')
  })

  it('formats same-day and cross-midnight ranges in capture-local wall time', () => {
    const same = [
      feat(1, '2026-08-28T14:10:00-06:00', 60),
      feat(2, '2026-08-28T14:11:01-06:00', 959),
    ]
    expect(buildFlightCatalog(same).groups.get('flight:1')?.label)
      .toBe('Flight · August 28, 2026 · 2:10–2:27 PM')

    const overnight = [feat(3, '2026-08-28T23:59:00-06:00', 660)]
    expect(buildFlightCatalog(overnight).groups.get('flight:3')?.label)
      .toBe('Flight · August 28, 2026, 11:59 PM – August 29, 2026, 12:10 AM')
  })
})

describe('groupFlightsByDate', () => {
  it('orders date buckets and flights by direction while clips remain chronological', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T11:00:00-06:00', 10),
      feat(4, '2026-08-28T11:00:11-06:00', 10),
      feat(5, '2026-08-29T09:00:00-06:00', 10),
    ]
    expect(flights(files, 'desc').map((g) => g.members.map((f) => f.properties.id)))
      .toEqual([[5], [3, 4], [1, 2]])
    expect(flights(files, 'asc').map((g) => g.members.map((f) => f.properties.id)))
      .toEqual([[1, 2], [3, 4], [5]])
  })

  it('keeps a cross-midnight flight intact under its start date', () => {
    const files = [
      feat(1, '2026-08-28T23:59:00-06:00', 90),
      feat(2, '2026-08-29T00:00:31-06:00', 60),
    ]
    const groups = groupFlightsByDate(files, buildFlightCatalog(files), 'day', 'asc')
    expect(groups.map((group) => group.label)).toEqual(['August 28, 2026'])
    expect(groups[0].flights[0].members.map((f) => f.properties.id)).toEqual([1, 2])
  })

  it('preserves full membership when the viewport omits an intermediate clip', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T10:00:22-06:00', 10),
    ]
    const [group] = flights(files, 'asc', [files[0], files[2]])
    expect(group.visibleFiles.map((f) => f.properties.id)).toEqual([1, 3])
    expect(group.members.map((f) => f.properties.id)).toEqual([1, 2, 3])

    const selection = selectionForFlight(group, 3)
    expect(selection.files.map((f) => f.properties.id)).toEqual([1, 2, 3])
    expect(selection.index).toBe(2)
    expect(selection.flight?.key).toBe('flight:1')
  })

  it('emits distinct flight headers and boundary metadata with viewport counts', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T10:00:22-06:00', 10),
    ]
    const groups = groupFlightsByDate(
      [files[0], files[2]],
      buildFlightCatalog(files),
      'month',
      'asc',
    )
    const rows = buildFlightRowModel(groups, 1)
    expect(rows.map((row) => row.kind)).toEqual([
      'date-header', 'flight-header', 'thumbs', 'thumbs',
    ])
    expect(rows[1]).toMatchObject({ visibleCount: 2, totalCount: 3 })
    expect(rows[1]).toMatchObject({ flightKey: 'flight:1' })
    expect(rows[2]).toMatchObject({ flightPosition: 'first' })
    expect(rows[3]).toMatchObject({ flightPosition: 'last' })
    expect(flightHeaderRowIndex(rows, 'flight:1')).toBe(1)
    expect(flightHeaderRowIndex(rows, 'flight:missing')).toBe(-1)
  })
})

describe('flight viewer selection', () => {
  it('opens a map-selected member against the complete catalog flight', () => {
    const files = [
      feat(1, '2026-08-28T10:00:00-06:00', 10),
      feat(2, '2026-08-28T10:00:11-06:00', 10),
      feat(3, '2026-08-28T10:00:22-06:00', 10),
    ]
    const selection = selectionForCatalogFlight(buildFlightCatalog(files), 3)

    expect(selection?.files.map((f) => f.properties.id)).toEqual([1, 2, 3])
    expect(selection?.index).toBe(2)
    expect(selection?.flight).toMatchObject({ key: 'flight:1' })
  })

  it('returns no flight selection for non-flight captures', () => {
    const photo = feat(1, '2026-08-28T10:00:00-06:00', null, {
      filename: 'DJI_0001.JPG',
      media_type: 'photo',
      codec: null,
      gps_source: 'exif',
      path: 'photos/1.jpg',
    })

    expect(selectionForCatalogFlight(buildFlightCatalog([photo]), photo.properties.id)).toBeNull()
  })
})

describe('flight subgroup filter state', () => {
  it('stays enabled across date granularity changes and restores exact media chips', () => {
    let state = initialGroupingFilterState()
    state = toggleGroupingCategory(state, 'photo')
    state = toggleGroupingCategory(state, 'hyperlapse')
    expect([...state.enabled]).toEqual(['video', 'panorama'])

    state = setFlightSubgroups(state, true)
    expect([...state.enabled]).toEqual(['video'])
    expect(toggleGroupingCategory(state, 'photo')).toBe(state)

    state = changeGranularity(state, 'day')
    expect(state.subgroupFlights).toBe(true)
    expect(state.granularity).toBe('day')

    state = setFlightSubgroups(state, false)
    expect([...state.enabled]).toEqual(['video', 'panorama'])
    expect(state.beforeFlight).toBeNull()
  })
})
