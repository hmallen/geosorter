import { describe, it, expect } from 'vitest'
import { mediaUrl, thumbUrl, previewUrl, posterUrl, videoUrl, listThumb, fetchInbox, framesUrl, fetchFrames, stitchUrl } from './api'

describe('media URL builders', () => {
  it('encodes each segment (spaces, commas) but keeps slashes', () => {
    expect(mediaUrl('A B/c,d.JPG')).toBe('/api/media/A%20B/c%2Cd.JPG')
  })

  it('prefixes by asset kind', () => {
    const p = 'X/y.jpg'
    expect(thumbUrl(p)).toBe('/api/thumb/X/y.jpg')
    expect(previewUrl(p)).toBe('/api/preview/X/y.jpg')
    expect(posterUrl(p)).toBe('/api/poster/X/y.jpg')
    expect(videoUrl(p)).toBe('/api/video/X/y.jpg')
  })

  it('builds the stitched-panorama hero URL from a file id', () => {
    expect(stitchUrl(7)).toBe('/api/stitch/7')
  })
})

describe('listThumb', () => {
  it('uses the video poster for videos and the image thumb for photos', () => {
    expect(listThumb('video', 'X/y.mp4')).toBe(posterUrl('X/y.mp4'))
    expect(listThumb('photo', 'X/y.jpg')).toBe(thumbUrl('X/y.jpg'))
  })
})

describe('fetchInbox', () => {
  it('returns the parsed counts', async () => {
    const fetchFn = (async () => ({
      ok: true, status: 200, json: async () => ({ files: 7, captures: 3 }),
    })) as unknown as typeof fetch
    expect(await fetchInbox(fetchFn)).toEqual({ files: 7, captures: 3 })
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(fetchInbox(fetchFn)).rejects.toThrow(/inbox fetch failed: 500/)
  })
})

describe('frames gallery (B10)', () => {
  it('builds the frames URL from a file id', () => {
    expect(framesUrl(42)).toBe('/api/frames/42')
  })

  it('returns the parsed frame relpaths', async () => {
    const fetchFn = (async () => ({
      ok: true, status: 200,
      json: async () => ({ frames: ['P/hl_frames/HYPERLAPSE_0001.JPG'] }),
    })) as unknown as typeof fetch
    expect(await fetchFrames(7, fetchFn)).toEqual(['P/hl_frames/HYPERLAPSE_0001.JPG'])
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 404 }) as Response) as unknown as typeof fetch
    await expect(fetchFrames(7, fetchFn)).rejects.toThrow(/frames fetch failed: 404/)
  })
})
