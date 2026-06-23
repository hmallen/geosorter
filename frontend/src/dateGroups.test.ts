import { describe, expect, it } from 'vitest'
import { parseParts, groupFeatures, buildRowModel } from './dateGroups'
import type { LibraryFeature } from './types'

// Minimal LibraryFeature factory: grouping reads only the two date fields, but we
// type it as a real feature so the helpers' signatures stay honest.
function feat(
  id: number,
  capture_ts_local: string | null,
  local_date: string | null = null,
): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: {
      id,
      filename: `f${id}.jpg`,
      place_string: null,
      local_date,
      capture_ts_local,
      media_type: 'photo',
      codec: null,
      gps_source: 'exif',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      path: `p/${id}.jpg`,
    },
  }
}

describe('parseParts', () => {
  it('parses Y/M/D from capture_ts_local', () => {
    expect(parseParts(feat(1, '2024-04-12T14:34:22-06:00').properties)).toEqual({
      year: 2024,
      month: 4,
      day: 12,
    })
  })

  it('falls back to local_date when capture_ts_local is null', () => {
    expect(parseParts(feat(1, null, '2024-07-04').properties)).toEqual({
      year: 2024,
      month: 7,
      day: 4,
    })
  })

  it('falls back to local_date when capture_ts_local is unparseable', () => {
    expect(parseParts(feat(1, 'not-a-ts', '2024-07-04').properties)).toEqual({
      year: 2024,
      month: 7,
      day: 4,
    })
  })

  it('returns null when both date fields are null/unparseable', () => {
    expect(parseParts(feat(1, null, null).properties)).toBeNull()
    expect(parseParts(feat(1, 'nope', 'also-nope').properties)).toBeNull()
  })
})

describe('groupFeatures', () => {
  it('buckets by month with a "Month YYYY" label', () => {
    const groups = groupFeatures(
      [feat(1, '2024-04-12T10:00:00-06:00'), feat(2, '2024-04-28T10:00:00-06:00')],
      'month',
    )
    expect(groups).toHaveLength(1)
    expect(groups[0].label).toBe('April 2024')
    // Default desc sorts within-group newest-first, so the 28th (id 2) precedes the 12th.
    expect(groups[0].files.map((f) => f.properties.id)).toEqual([2, 1])
  })

  it('buckets by day with a "Month D, YYYY" label', () => {
    const groups = groupFeatures(
      [feat(1, '2024-04-12T10:00:00-06:00'), feat(2, '2024-04-13T10:00:00-06:00')],
      'day',
    )
    expect(groups.map((g) => g.label)).toEqual(['April 13, 2024', 'April 12, 2024'])
  })

  it('buckets by year with a "YYYY" label', () => {
    const groups = groupFeatures(
      [feat(1, '2024-04-12T10:00:00-06:00'), feat(2, '2025-01-02T10:00:00-06:00')],
      'year',
    )
    expect(groups.map((g) => g.label)).toEqual(['2025', '2024'])
  })

  it('orders groups newest-first', () => {
    const groups = groupFeatures(
      [
        feat(1, '2024-01-15T10:00:00-06:00'),
        feat(2, '2024-06-15T10:00:00-06:00'),
        feat(3, '2023-12-15T10:00:00-06:00'),
      ],
      'month',
    )
    expect(groups.map((g) => g.label)).toEqual(['June 2024', 'January 2024', 'December 2023'])
  })

  it('orders files within a group newest-first by default (desc)', () => {
    const groups = groupFeatures(
      [
        feat(1, '2024-04-12T10:00:00-06:00'),
        feat(2, '2024-04-28T10:00:00-06:00'),
        feat(3, '2024-04-05T10:00:00-06:00'),
      ],
      'month',
    )
    expect(groups[0].files.map((f) => f.properties.id)).toEqual([2, 1, 3])
  })

  it("dir='asc' orders groups oldest-first", () => {
    const groups = groupFeatures(
      [
        feat(1, '2024-01-15T10:00:00-06:00'),
        feat(2, '2024-06-15T10:00:00-06:00'),
        feat(3, '2023-12-15T10:00:00-06:00'),
      ],
      'month',
      'asc',
    )
    expect(groups.map((g) => g.label)).toEqual(['December 2023', 'January 2024', 'June 2024'])
  })

  it("dir='asc' orders files within a group oldest-first", () => {
    const groups = groupFeatures(
      [
        feat(1, '2024-04-12T10:00:00-06:00'),
        feat(2, '2024-04-28T10:00:00-06:00'),
        feat(3, '2024-04-05T10:00:00-06:00'),
      ],
      'month',
      'asc',
    )
    expect(groups[0].files.map((f) => f.properties.id)).toEqual([3, 1, 2])
  })

  it('sorts within-group by the validated date when capture_ts_local is unparseable', () => {
    const groups = groupFeatures(
      [
        feat(1, '2024-04-25T10:00:00-06:00'),
        feat(2, 'corrupt-ts', '2024-04-10'),
      ],
      'month',
    )
    // id 2 is bucketed into April 2024 via the local_date fallback (parseParts), so its
    // within-group sort must use that same validated date — desc puts the 25th (id 1)
    // before the 10th (id 2), NOT ordered by the raw 'corrupt-ts' string.
    expect(groups).toHaveLength(1)
    expect(groups[0].files.map((f) => f.properties.id)).toEqual([1, 2])
  })

  it('collects undated captures into a trailing "Unknown date" group', () => {
    const groups = groupFeatures(
      [feat(1, '2024-04-12T10:00:00-06:00'), feat(2, null, null), feat(3, null, null)],
      'month',
    )
    expect(groups.map((g) => g.label)).toEqual(['April 2024', 'Unknown date'])
    expect(groups[1].files.map((f) => f.properties.id)).toEqual([2, 3])
  })

  it("keeps the 'Unknown date' group trailing in ascending order", () => {
    const groups = groupFeatures(
      [feat(1, '2024-06-15T10:00:00-06:00'), feat(2, null, null), feat(3, '2024-01-15T10:00:00-06:00')],
      'month',
      'asc',
    )
    expect(groups.map((g) => g.label)).toEqual(['January 2024', 'June 2024', 'Unknown date'])
  })

  it('returns an empty array for no files', () => {
    expect(groupFeatures([], 'month')).toEqual([])
  })
})

describe('buildRowModel', () => {
  it('emits one header row per group then ceil(n/columns) thumb rows, never spanning groups', () => {
    const groups = groupFeatures(
      [
        // Newest-first within April (desc default) keeps ids 1,2,3 in row order.
        feat(1, '2024-04-03T10:00:00-06:00'),
        feat(2, '2024-04-02T10:00:00-06:00'),
        feat(3, '2024-04-01T10:00:00-06:00'),
        feat(4, '2024-03-01T10:00:00-06:00'),
      ],
      'month',
    )
    // April has 3 files, March has 1; with 2 columns -> April: header + 2 thumb rows
    // (2 then 1), March: header + 1 thumb row.
    const rows = buildRowModel(groups, 2)
    expect(rows.map((r) => r.kind)).toEqual(['header', 'thumbs', 'thumbs', 'header', 'thumbs'])
    expect(rows[0]).toMatchObject({ kind: 'header', label: 'April 2024' })
    const thumbRow1 = rows[1]
    const thumbRow2 = rows[2]
    if (thumbRow1.kind !== 'thumbs' || thumbRow2.kind !== 'thumbs') throw new Error('expected thumbs')
    expect(thumbRow1.files.map((f) => f.properties.id)).toEqual([1, 2])
    expect(thumbRow2.files.map((f) => f.properties.id)).toEqual([3])
    expect(rows[3]).toMatchObject({ kind: 'header', label: 'March 2024' })
  })

  it('treats columns < 1 as 1', () => {
    const groups = groupFeatures([feat(1, '2024-04-01T10:00:00-06:00')], 'month')
    const rows = buildRowModel(groups, 0)
    expect(rows.map((r) => r.kind)).toEqual(['header', 'thumbs'])
  })

  it('returns an empty array for no groups', () => {
    expect(buildRowModel([], 3)).toEqual([])
  })
})
