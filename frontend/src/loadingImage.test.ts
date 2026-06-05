import { describe, it, expect } from 'vitest'
import { MAX_IMG_RETRIES, retryDelayMs } from './loadingImage'

describe('retryDelayMs', () => {
  it('grows exponentially from 400ms', () => {
    expect(retryDelayMs(1)).toBe(400)
    expect(retryDelayMs(2)).toBe(800)
    expect(retryDelayMs(3)).toBe(1600)
    expect(retryDelayMs(4)).toBe(3200)
  })

  it('caps at 4000ms', () => {
    expect(retryDelayMs(5)).toBe(4000)
    expect(retryDelayMs(10)).toBe(4000)
  })

  it('exposes a small bounded retry count', () => {
    expect(MAX_IMG_RETRIES).toBeGreaterThan(0)
    expect(MAX_IMG_RETRIES).toBeLessThanOrEqual(6)
  })
})
