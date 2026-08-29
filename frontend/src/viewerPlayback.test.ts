import { describe, expect, it } from 'vitest'
import { nextFlightAutoplayIndex } from './viewerPlayback'

describe('nextFlightAutoplayIndex', () => {
  it('continues chronologically from the selected clip inside a flight', () => {
    expect(nextFlightAutoplayIndex(0, 4, true)).toBe(1)
    expect(nextFlightAutoplayIndex(2, 4, true)).toBe(3)
  })

  it('stops on the final flight clip instead of looping', () => {
    expect(nextFlightAutoplayIndex(3, 4, true)).toBeNull()
  })

  it('does not advance ordinary non-flight lightboxes', () => {
    expect(nextFlightAutoplayIndex(0, 4, false)).toBeNull()
  })
})
