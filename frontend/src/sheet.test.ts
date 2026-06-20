import { describe, it, expect } from 'vitest'
import { SHEET_SNAPS, clampFraction, nearestSnap, cycleSnap } from './sheet'

describe('SHEET_SNAPS', () => {
  it('is a strictly ascending list of viewport-height fractions in (0,1]', () => {
    expect(SHEET_SNAPS.length).toBeGreaterThanOrEqual(2)
    for (let i = 1; i < SHEET_SNAPS.length; i++) {
      expect(SHEET_SNAPS[i]).toBeGreaterThan(SHEET_SNAPS[i - 1])
    }
    expect(SHEET_SNAPS[0]).toBeGreaterThan(0)
    expect(SHEET_SNAPS[SHEET_SNAPS.length - 1]).toBeLessThanOrEqual(1)
  })
})

describe('clampFraction', () => {
  it('bounds a fraction to the [min, max] snap range', () => {
    const min = SHEET_SNAPS[0]
    const max = SHEET_SNAPS[SHEET_SNAPS.length - 1]
    expect(clampFraction(-1)).toBe(min)
    expect(clampFraction(0)).toBe(min)
    expect(clampFraction(5)).toBe(max)
    expect(clampFraction(min + (max - min) / 2)).toBeCloseTo(min + (max - min) / 2)
  })
})

describe('nearestSnap', () => {
  it('returns the closest snap value to a fraction', () => {
    expect(nearestSnap(SHEET_SNAPS[0] - 0.5)).toBe(SHEET_SNAPS[0])
    expect(nearestSnap(SHEET_SNAPS[SHEET_SNAPS.length - 1] + 0.5)).toBe(
      SHEET_SNAPS[SHEET_SNAPS.length - 1],
    )
  })

  it('snaps a midpoint-ish value to the nearer neighbour', () => {
    const a = SHEET_SNAPS[0]
    const b = SHEET_SNAPS[1]
    // Just below the midpoint snaps down to a, just above snaps up to b.
    expect(nearestSnap(a + (b - a) * 0.49)).toBe(a)
    expect(nearestSnap(a + (b - a) * 0.51)).toBe(b)
  })
})

describe('cycleSnap', () => {
  it('advances to the next snap and wraps from the last back to the first', () => {
    expect(cycleSnap(SHEET_SNAPS[0])).toBe(SHEET_SNAPS[1])
    expect(cycleSnap(SHEET_SNAPS[SHEET_SNAPS.length - 1])).toBe(SHEET_SNAPS[0])
  })

  it('cycles from the nearest snap when given an off-snap value', () => {
    // A value closest to the middle snap cycles to the one after it.
    const mid = SHEET_SNAPS[1]
    const next = SHEET_SNAPS[2] ?? SHEET_SNAPS[0]
    expect(cycleSnap(mid + 0.001)).toBe(next)
  })
})
