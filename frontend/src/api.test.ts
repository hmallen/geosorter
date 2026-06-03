import { describe, it, expect } from 'vitest'
import { mediaUrl, thumbUrl, previewUrl, posterUrl, videoUrl, listThumb, fetchInbox } from './api'

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
