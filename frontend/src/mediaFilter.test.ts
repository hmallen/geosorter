import { describe, expect, it } from 'vitest'
import { categoryOf, filterByCategories, type MediaCategory } from './mediaFilter'
import type { FeatureProps, LibraryFeature } from './types'

function props(over: Partial<FeatureProps>): FeatureProps {
  return {
    id: 1,
    filename: 'f.jpg',
    place_string: null,
    local_date: null,
    capture_ts_local: null,
    media_type: 'photo',
    codec: null,
    duration_s: null,
    gps_source: 'exif',
    capture_kind: null,
    frame_count: null,
    star_rating: null,
    stitch_status: null,
    stitch_projection: null,
    path: 'p/f.jpg',
    ...over,
  }
}

function feat(over: Partial<FeatureProps>): LibraryFeature {
  return { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] }, properties: props(over) }
}

describe('categoryOf', () => {
  it('classifies a panorama (capture_kind wins over media_type)', () => {
    expect(categoryOf(props({ capture_kind: 'panorama', media_type: 'photo' }))).toBe('panorama')
  })

  it('classifies a hyperlapse render regardless of its media_type', () => {
    expect(categoryOf(props({ capture_kind: 'hyperlapse', media_type: 'video' }))).toBe('hyperlapse')
    expect(categoryOf(props({ capture_kind: 'hyperlapse', media_type: 'photo' }))).toBe('hyperlapse')
  })

  it('classifies a plain video', () => {
    expect(categoryOf(props({ capture_kind: null, media_type: 'video' }))).toBe('video')
  })

  it('classifies a plain photo', () => {
    expect(categoryOf(props({ capture_kind: null, media_type: 'photo' }))).toBe('photo')
  })
})

describe('filterByCategories', () => {
  const files = [
    feat({ id: 1, capture_kind: 'panorama' }),
    feat({ id: 2, capture_kind: 'hyperlapse', media_type: 'video' }),
    feat({ id: 3, media_type: 'video' }),
    feat({ id: 4, media_type: 'photo' }),
  ]

  it('keeps only captures whose category is enabled', () => {
    const enabled = new Set<MediaCategory>(['photo', 'video'])
    expect(filterByCategories(files, enabled).map((f) => f.properties.id)).toEqual([3, 4])
  })

  it('keeps everything when all four categories are enabled', () => {
    const enabled = new Set<MediaCategory>(['photo', 'video', 'panorama', 'hyperlapse'])
    expect(filterByCategories(files, enabled).map((f) => f.properties.id)).toEqual([1, 2, 3, 4])
  })

  it('returns an empty array when no category is enabled', () => {
    expect(filterByCategories(files, new Set<MediaCategory>())).toEqual([])
  })
})
