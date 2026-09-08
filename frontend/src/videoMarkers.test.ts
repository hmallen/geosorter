import { describe, expect, it, vi } from 'vitest'
import { deleteVideoMarker, fetchVideoMarkers, formatMarkerTime, groupMarkers, parseMarkerTime, saveVideoMarker, type VideoMarker } from './videoMarkers'
import type { LibraryFeature } from './types'

const marker = (id: number, time_s: number, file_id = 1, note = ''): VideoMarker =>
  ({ id, time_s, file_id, note, created_at: '', updated_at: '' })

describe('marker timestamps', () => {
  it.each([['0', 0], ['1.125', 1.125], [' 01:02.500 ', 62.5], ['1:02:03.125', 3723.125], ['90', 90]])('parses %s', (text, time) => {
    expect(parseMarkerTime(text)).toBe(time)
  })
  it.each(['', '-1', 'NaN', 'Infinity', '1:60', '1:60:00', '1::2', '1:2:3:4', '1e2', '1.5:00'])('rejects %s', (text) => {
    expect(parseMarkerTime(text)).toBeNull()
  })
  it.each([0, 0.001, 9, 10, 59.999, 60, 3600, 3723.125])('round trips %s', (time) => {
    expect(parseMarkerTime(formatMarkerTime(time))).toBe(time)
  })
})

it('groups across the full library, filters notes, sorts dates and timestamps, and drops absent files', () => {
  const features = [1, 2].map((id) => ({ properties: { id, filename: `${id}.mp4`, capture_ts_local: `2026-09-0${id}` } }) as LibraryFeature)
  const groups = groupMarkers([marker(1, 20), marker(2, 10), marker(3, 5, 2, 'mountain'), marker(4, 0, 99)], features, '')
  expect(groups.map((g) => g.file.properties.id)).toEqual([2, 1])
  expect(groups[1].markers.map((m) => m.id)).toEqual([2, 1])
  expect(groupMarkers(groups.flatMap((g) => g.markers), features, 'MOUNTAIN')[0].markers[0].id).toBe(3)
  expect(groupMarkers(groups.flatMap((g) => g.markers), features, '1.mp4')[0].markers).toHaveLength(2)
})

it('uses the supplied authenticated fetch and preserves blank notes and fractional timestamps', async () => {
  const fetchFn = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(marker(1, 1.125))))
  await saveVideoMarker(fetchFn, 1, { time_s: 1.125, note: '' })
  expect(fetchFn).toHaveBeenCalledWith('/api/video-markers', expect.objectContaining({ method: 'POST', body: '{"time_s":1.125,"note":"","file_id":1}' }))
  fetchFn.mockResolvedValue(new Response(JSON.stringify(marker(7, 0))))
  await saveVideoMarker(fetchFn, 1, { time_s: 0, note: 'edit' }, 7)
  expect(fetchFn).toHaveBeenLastCalledWith('/api/video-markers/7', expect.objectContaining({ method: 'PATCH' }))
  fetchFn.mockResolvedValue(new Response(null, { status: 204 }))
  await expect(deleteVideoMarker(fetchFn, 7)).resolves.toBeUndefined()
})

it('sorts fetched markers and surfaces server errors for retry', async () => {
  const fetchFn = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ markers: [marker(1, 10), marker(2, 0)] })))
  expect((await fetchVideoMarkers(fetchFn, 1)).map((m) => m.id)).toEqual([2, 1])
  expect(fetchFn).toHaveBeenCalledWith('/api/video-markers?file_id=1')
  fetchFn.mockResolvedValue(new Response(JSON.stringify({ detail: 'Timestamp exceeds video duration' }), { status: 422 }))
  await expect(saveVideoMarker(fetchFn, 1, { time_s: 100, note: '' })).rejects.toThrow('Timestamp exceeds')
  fetchFn.mockResolvedValue(new Response('offline', { status: 503 }))
  await expect(fetchVideoMarkers(fetchFn)).rejects.toThrow('503')
})
