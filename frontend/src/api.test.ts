import { describe, it, expect } from 'vitest'
import { mediaUrl, thumbUrl, previewUrl, posterUrl, videoUrl, listThumb, fetchInbox, framesUrl, fetchFrames, stitchUrl, fetchLibrary, fetchQuarantine, placeSearch, trackUrl, fetchTrack, fetchDuplicates, dismissDuplicates, setFavorite } from './api'

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

describe('fetchLibrary (ETag conditional GET)', () => {
  it('sends If-None-Match with the prior ETag and returns the new ETag on 200', async () => {
    let sentHeaders: Record<string, string> | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      sentHeaders = init?.headers as Record<string, string>
      return {
        ok: true,
        status: 200,
        headers: { get: (k: string) => (k.toLowerCase() === 'etag' ? 'W/"lib-3-2"' : null) },
        json: async () => ({ type: 'FeatureCollection', features: [] }),
      }
    }) as unknown as typeof fetch
    const res = await fetchLibrary(fetchFn, 'W/"lib-1-1"')
    expect(sentHeaders?.['If-None-Match']).toBe('W/"lib-1-1"')
    expect(res.notModified).toBe(false)
    expect(res.etag).toBe('W/"lib-3-2"')
    expect(res.fc?.features).toEqual([])
  })

  it('returns notModified on 304 without parsing a body, preserving the prior ETag', async () => {
    const fetchFn = (async () => ({
      ok: false,
      status: 304,
      headers: { get: () => null },
      json: async () => { throw new Error('must not parse a 304 body') },
    })) as unknown as typeof fetch
    const res = await fetchLibrary(fetchFn, 'W/"lib-1-1"')
    expect(res.notModified).toBe(true)
    expect(res.fc).toBeNull()
    expect(res.etag).toBe('W/"lib-1-1"')
  })

  it('omits If-None-Match when there is no prior ETag', async () => {
    let sentHeaders: Record<string, string> | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      sentHeaders = init?.headers as Record<string, string>
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'W/"lib-1-1"' },
        json: async () => ({ type: 'FeatureCollection', features: [] }),
      }
    }) as unknown as typeof fetch
    await fetchLibrary(fetchFn)
    expect(sentHeaders?.['If-None-Match']).toBeUndefined()
  })

  it('throws on a non-OK, non-304 response', async () => {
    const fetchFn = (async () => ({
      ok: false, status: 500, headers: { get: () => null },
    })) as unknown as typeof fetch
    await expect(fetchLibrary(fetchFn)).rejects.toThrow(/library fetch failed: 500/)
  })
})

describe('fetchQuarantine', () => {
  it('returns the parsed no-GPS feature list', async () => {
    const item = {
      id: 5, filename: 'q.JPG', media_type: 'photo', date: '2024-07-04',
      capture_kind: null, frame_count: null, path: '_no-gps/q.JPG',
    }
    const fetchFn = (async () => ({
      ok: true, status: 200, json: async () => ({ features: [item] }),
    })) as unknown as typeof fetch
    expect(await fetchQuarantine(fetchFn)).toEqual([item])
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(fetchQuarantine(fetchFn)).rejects.toThrow(/quarantine fetch failed: 500/)
  })
})

describe('placeSearch', () => {
  it('short-circuits a blank query to [] without fetching', async () => {
    const fetchFn = (async () => { throw new Error('must not fetch') }) as unknown as typeof fetch
    expect(await placeSearch('   ', fetchFn)).toEqual([])
  })

  it('encodes the query and returns the ranked matches', async () => {
    const match = {
      geonameid: 5419384, name: 'Denver', place_string: 'Denver, Colorado, United States',
      lat: 39.739, lon: -104.985, feature_class: 'P',
    }
    let calledUrl: string | undefined
    const fetchFn = (async (url: string) => {
      calledUrl = url
      return { ok: true, status: 200, json: async () => ({ results: [match] }) } as Response
    }) as unknown as typeof fetch
    expect(await placeSearch('Denver, CO', fetchFn)).toEqual([match])
    expect(calledUrl).toBe('/api/place-search?q=Denver%2C%20CO')
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(placeSearch('Moab', fetchFn)).rejects.toThrow(/place search failed: 500/)
  })
})

describe('fetchDuplicates', () => {
  it('returns the parsed duplicate items', async () => {
    const item = {
      id: 3, filename: 'DJI_0001.MP4', source_path: 'card1/DJI_0001.MP4',
      matched_path: 'Moab, Utah/2024-07-04/DJI_0001.MP4', matched_file_id: 12,
      sha256: 'abc', first_seen_at: '2026-07-01 10:00:00', missing: false,
    }
    const fetchFn = (async () => ({
      ok: true, status: 200, json: async () => ({ items: [item], count: 1 }),
    })) as unknown as typeof fetch
    expect(await fetchDuplicates(fetchFn)).toEqual([item])
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(fetchDuplicates(fetchFn)).rejects.toThrow(/duplicates fetch failed: 500/)
  })
})

describe('dismissDuplicates', () => {
  it('POSTs the ids and returns the parsed result', async () => {
    let calledUrl: string | undefined
    let sentInit: RequestInit | undefined
    const fetchFn = (async (url: string, init?: RequestInit) => {
      calledUrl = url
      sentInit = init
      return {
        ok: true, status: 200,
        json: async () => ({ dismissed: 2, skipped: 0, failures: [] }),
      } as Response
    }) as unknown as typeof fetch
    expect(await dismissDuplicates(fetchFn, [3, 4])).toEqual({
      dismissed: 2, skipped: 0, failures: [],
    })
    expect(calledUrl).toBe('/api/duplicates/dismiss')
    expect(sentInit?.method).toBe('POST')
    expect(JSON.parse(sentInit?.body as string)).toEqual({ ids: [3, 4] })
  })

  it('throws on a non-OK response (e.g. 409 while a job runs)', async () => {
    const fetchFn = (async () => ({ ok: false, status: 409 }) as Response) as unknown as typeof fetch
    await expect(dismissDuplicates(fetchFn, [1])).rejects.toThrow(/duplicates dismiss failed: 409/)
  })

  it('surfaces the server detail.message on an error response', async () => {
    const fetchFn = (async () => ({
      ok: false, status: 409,
      json: async () => ({
        detail: { message: 'a destructive job is already running', blocking_job_id: 3 },
      }),
    })) as unknown as typeof fetch
    await expect(dismissDuplicates(fetchFn, [1])).rejects.toThrow(
      'duplicates dismiss failed: 409 — a destructive job is already running',
    )
  })

  it('surfaces a bare-string detail', async () => {
    const fetchFn = (async () => ({
      ok: false, status: 400, json: async () => ({ detail: 'no inbox configured' }),
    })) as unknown as typeof fetch
    await expect(dismissDuplicates(fetchFn, [1])).rejects.toThrow(
      'duplicates dismiss failed: 400 — no inbox configured',
    )
  })

  it('falls back to the status-only message when the error body is not JSON', async () => {
    const fetchFn = (async () => ({
      ok: false, status: 502, json: async () => { throw new Error('not json') },
    })) as unknown as typeof fetch
    await expect(dismissDuplicates(fetchFn, [1])).rejects.toThrow(
      'duplicates dismiss failed: 502',
    )
  })
})

describe('setFavorite', () => {
  it('POSTs the file id + desired state and returns the echo', async () => {
    let calledUrl: string | undefined
    let sentInit: RequestInit | undefined
    const fetchFn = (async (url: string, init?: RequestInit) => {
      calledUrl = url
      sentInit = init
      return {
        ok: true, status: 200, json: async () => ({ file_id: 7, favorite: true }),
      } as Response
    }) as unknown as typeof fetch
    expect(await setFavorite(fetchFn, 7, true)).toEqual({ file_id: 7, favorite: true })
    expect(calledUrl).toBe('/api/favorite')
    expect(sentInit?.method).toBe('POST')
    expect(JSON.parse(sentInit?.body as string)).toEqual({ file_id: 7, favorite: true })
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 404 }) as Response) as unknown as typeof fetch
    await expect(setFavorite(fetchFn, 999, false)).rejects.toThrow(/favorite update failed: 404/)
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

describe('flight track', () => {
  it('builds the track URL and decodes points plus timed samples', async () => {
    expect(trackUrl(42)).toBe('/api/track/42')
    const payload = {
      points: [[-105, 40], [-104, 41]],
      samples: [
        { time_s: 1.25, lon: -105, lat: 40 },
        { time_s: 2.25, lon: -104, lat: 41 },
      ],
    }
    const fetchFn = (async () => ({
      ok: true,
      status: 200,
      json: async () => payload,
    })) as unknown as typeof fetch
    expect(await fetchTrack(42, fetchFn)).toEqual(payload)
  })

  it('defaults missing additive samples for an older server payload', async () => {
    const fetchFn = (async () => ({
      ok: true,
      status: 200,
      json: async () => ({ points: [[-105, 40]] }),
    })) as unknown as typeof fetch
    expect(await fetchTrack(7, fetchFn)).toEqual({
      points: [[-105, 40]],
      samples: [],
    })
  })

  it('throws on a non-OK response', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(fetchTrack(7, fetchFn)).rejects.toThrow(/track fetch failed: 500/)
  })
})
