// Typed helpers for the B6 HTTP API: media-URL builders + the library fetch.
import type { LibraryFC } from './types'
import type { InboxGroup } from './inboxTree'

// Encode each path segment but preserve the separators, so a library-relative
// path like "Boulder, Colorado/2024-07-04/x.JPG" becomes a valid URL the
// backend's {relpath:path} route decodes back to the same path.
const enc = (path: string): string => path.split('/').map(encodeURIComponent).join('/')

export const mediaUrl = (path: string): string => `/api/media/${enc(path)}`
export const thumbUrl = (path: string): string => `/api/thumb/${enc(path)}`
export const previewUrl = (path: string): string => `/api/preview/${enc(path)}`
export const posterUrl = (path: string): string => `/api/poster/${enc(path)}`
export const videoUrl = (path: string): string => `/api/video/${enc(path)}`

// File-list tile image: the /api/thumb route renders images with Pillow, which
// can't open a video — so videos use their ffmpeg poster frame instead.
export const listThumb = (mediaType: 'photo' | 'video', path: string): string =>
  mediaType === 'video' ? posterUrl(path) : thumbUrl(path)

// Result of a (conditional) library fetch. On a 304 the server confirmed the
// prior ETag is still current: `fc` is null and the caller keeps its stale
// features visible (no blank map on reload).
export interface LibraryResult {
  fc: LibraryFC | null
  etag: string | null
  notModified: boolean
}

export async function fetchLibrary(
  fetchFn: typeof fetch = fetch,
  etag?: string | null,
): Promise<LibraryResult> {
  const headers: Record<string, string> = {}
  if (etag) headers['If-None-Match'] = etag
  const resp = await fetchFn('/api/library', { headers })
  if (resp.status === 304) return { fc: null, etag: etag ?? null, notModified: true }
  if (!resp.ok) throw new Error(`library fetch failed: ${resp.status}`)
  const fc = (await resp.json()) as LibraryFC
  return { fc, etag: resp.headers.get('ETag'), notModified: false }
}

// Hyperlapse source-frame gallery (B10): list a render's frame relpaths, then
// build a thumbnail URL per frame with the existing /api/thumb route (frames are
// JPEGs, so Pillow renders them directly).
export const framesUrl = (id: number): string => `/api/frames/${id}`

export async function fetchFrames(
  id: number,
  fetchFn: typeof fetch = fetch,
): Promise<string[]> {
  const resp = await fetchFn(framesUrl(id))
  if (!resp.ok) throw new Error(`frames fetch failed: ${resp.status}`)
  return ((await resp.json()) as { frames: string[] }).frames
}

// Panorama stitched hero (B13): the cached 360 equirectangular for a panorama
// primary. 404 until generated (the lightbox falls back to the tile gallery).
export const stitchUrl = (id: number): string => `/api/stitch/${id}`

// Inbox counter (B8): how much is waiting for the next organize run.
export interface InboxCount {
  files: number
  captures: number
}

export async function fetchInbox(fetchFn: typeof fetch = fetch): Promise<InboxCount> {
  const resp = await fetchFn('/api/inbox')
  if (!resp.ok) throw new Error(`inbox fetch failed: ${resp.status}`)
  return (await resp.json()) as InboxCount
}

// The inbox's capture groups for the import-selection panel (one entry per group).
export async function fetchInboxList(fetchFn: typeof fetch = fetch): Promise<InboxGroup[]> {
  const resp = await fetchFn('/api/inbox/list')
  if (!resp.ok) throw new Error(`inbox list fetch failed: ${resp.status}`)
  return ((await resp.json()) as { groups: InboxGroup[] }).groups
}
