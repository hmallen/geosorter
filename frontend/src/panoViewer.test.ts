import { describe, expect, it } from 'vitest'
import { pickPanoViewer, resolvePanoViewer } from './panoViewer'

describe('pickPanoViewer', () => {
  it('routes a flat projection to the flat viewer', () => {
    expect(pickPanoViewer('flat')).toBe('flat')
  })

  it('routes an equirectangular projection to the sphere viewer', () => {
    expect(pickPanoViewer('equirectangular')).toBe('sphere')
  })

  it('defaults a null projection (legacy 360 hero) to the sphere viewer', () => {
    expect(pickPanoViewer(null)).toBe('sphere')
  })

  it('defaults an undefined projection to the sphere viewer', () => {
    expect(pickPanoViewer(undefined)).toBe('sphere')
  })

  it('defaults an unknown projection string to the sphere viewer', () => {
    expect(pickPanoViewer('')).toBe('sphere')
  })
})

describe('resolvePanoViewer', () => {
  it('prefers a non-empty in-session projection', () => {
    expect(resolvePanoViewer('flat', null)).toBe('flat')
    expect(resolvePanoViewer('equirectangular', 'flat')).toBe('sphere')
  })

  it("does NOT let an empty in-session projection ('' on a cache hit) mask the backfilled library value", () => {
    // The bug this guards: `?? ` kept '' and rendered a flat cache-hit pano as a sphere.
    expect(resolvePanoViewer('', 'flat')).toBe('flat')
  })

  it('falls back to the library projection when no in-session value', () => {
    expect(resolvePanoViewer(undefined, 'flat')).toBe('flat')
    expect(resolvePanoViewer(null, 'equirectangular')).toBe('sphere')
  })

  it('defaults to the sphere viewer when neither side has a projection', () => {
    expect(resolvePanoViewer('', null)).toBe('sphere')
    expect(resolvePanoViewer(undefined, undefined)).toBe('sphere')
  })
})
