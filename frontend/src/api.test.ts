import { describe, it, expect } from 'vitest'
import { mediaUrl, thumbUrl, previewUrl, posterUrl, videoUrl } from './api'

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
