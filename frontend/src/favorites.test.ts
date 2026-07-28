import { describe, it, expect } from 'vitest'
import { effectiveFavorite, filterFavorites } from './favorites'
import type { FeatureProps, LibraryFeature } from './types'

function feat(id: number, isFavorite?: boolean): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: { id, is_favorite: isFavorite } as unknown as FeatureProps,
  }
}

const none = new Map<number, boolean>()

describe('effectiveFavorite', () => {
  it('reads the server truth when there is no override', () => {
    expect(effectiveFavorite({ id: 1, is_favorite: true }, none)).toBe(true)
    expect(effectiveFavorite({ id: 1, is_favorite: false }, none)).toBe(false)
  })

  it('treats a missing is_favorite (older payloads) as not-favorite', () => {
    expect(effectiveFavorite({ id: 1 }, none)).toBe(false)
  })

  it('lets an override win in both directions', () => {
    expect(effectiveFavorite({ id: 1, is_favorite: false }, new Map([[1, true]]))).toBe(true)
    expect(effectiveFavorite({ id: 1, is_favorite: true }, new Map([[1, false]]))).toBe(false)
  })

  it('ignores overrides for other ids', () => {
    expect(effectiveFavorite({ id: 1, is_favorite: false }, new Map([[2, true]]))).toBe(false)
  })
})

describe('filterFavorites', () => {
  it('keeps features whose effective state is favorite', () => {
    const fs = [feat(1, true), feat(2, false), feat(3), feat(4, false)]
    const overrides = new Map<number, boolean>([
      [1, false], // un-favorited optimistically -> dropped
      [4, true], // favorited optimistically -> kept
    ])
    expect(filterFavorites(fs, overrides).map((f) => f.properties.id)).toEqual([4])
    expect(filterFavorites(fs, none).map((f) => f.properties.id)).toEqual([1])
  })

  it('returns [] for an empty library', () => {
    expect(filterFavorites([], none)).toEqual([])
  })
})
