import { describe, expect, it, vi } from 'vitest'
import { applyMarkerSeek, nextFlightAutoplayIndex } from './viewerPlayback'

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

function fakeVideo(readyState = 0) {
  return Object.assign(new EventTarget(), { readyState, duration: 60, currentTime: 0,
    pause: vi.fn(), play: vi.fn().mockResolvedValue(undefined) })
}

it('waits for metadata, applies a paused marker jump once, and clamps to duration', () => {
  const video = fakeVideo()
  const publish = vi.fn()
  applyMarkerSeek(video, 65, true, publish)
  expect(video.currentTime).toBe(0)
  video.readyState = 1
  video.dispatchEvent(new Event('loadedmetadata'))
  video.dispatchEvent(new Event('loadedmetadata'))
  expect(video.currentTime).toBe(60)
  expect(video.pause).toHaveBeenCalledOnce()
  expect(publish).toHaveBeenCalledExactlyOnceWith(60)
  expect(video.play).not.toHaveBeenCalled()
})

it('cancels a pending jump when navigation supersedes it', () => {
  const video = fakeVideo()
  const publish = vi.fn()
  const cancel = applyMarkerSeek(video, 12, true, publish)
  cancel()
  video.readyState = 1
  video.dispatchEvent(new Event('loadedmetadata'))
  expect(video.currentTime).toBe(0)
  expect(publish).not.toHaveBeenCalled()
})

it('permits repeated jumps to the same moment and preserves a playing request', () => {
  const video = fakeVideo(1)
  applyMarkerSeek(video, 12.125, false, vi.fn())()
  video.currentTime = 30
  applyMarkerSeek(video, 12.125, false, vi.fn())()
  expect(video.currentTime).toBe(12.125)
  expect(video.play).toHaveBeenCalledTimes(2)
  expect(video.pause).not.toHaveBeenCalled()
})
