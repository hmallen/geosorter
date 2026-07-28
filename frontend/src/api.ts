// Typed helpers for the B6 HTTP API: media-URL builders + the library fetch.
import type {
  AltitudeRef,
  DuplicateItem,
  FlightTrack,
  LibraryFC,
  PlaceResult,
  QuarantineItem,
} from './types'
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

// Flight track: the legacy route points plus SRT-clock-aligned samples used to
// synchronize the active drone marker (and its altitude readout) with
// video.currentTime. The wire field is snake_case `altitude_ref`; it is renamed
// here so the rest of the app sees one camelCase FlightTrack shape.
export const trackUrl = (id: number): string => `/api/track/${id}`

export async function fetchTrack(
  id: number,
  fetchFn: typeof fetch = fetch,
): Promise<FlightTrack> {
  const resp = await fetchFn(trackUrl(id))
  if (!resp.ok) throw new Error(`track fetch failed: ${resp.status}`)
  const payload = (await resp.json()) as Partial<FlightTrack> & {
    altitude_ref?: AltitudeRef | null
  }
  return {
    points: payload.points ?? [],
    samples: payload.samples ?? [],
    altitudeRef: payload.altitude_ref ?? null,
  }
}

// Panorama stitched hero (B13): the cached 360 equirectangular for a panorama
// primary. 404 until generated (the lightbox falls back to the tile gallery).
export const stitchUrl = (id: number): string => `/api/stitch/${id}`

// Instant raw-tile collage placeholder for a panorama (m-frontend-pano-ux): a
// cheap Pillow-composed grid of the raw tiles, generated on first request (no
// Hugin) and shown the moment the lightbox opens, while the optional 360 stitch
// is absent or still running.
export const collageUrl = (id: number): string => `/api/collage/${id}`

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

// No-GPS (quarantined) captures awaiting a manual location (for the No-GPS panel).
export async function fetchQuarantine(
  fetchFn: typeof fetch = fetch,
): Promise<QuarantineItem[]> {
  const resp = await fetchFn('/api/quarantine')
  if (!resp.ok) throw new Error(`quarantine fetch failed: ${resp.status}`)
  return ((await resp.json()) as { features: QuarantineItem[] }).features
}

// Duplicate-review backlog: inbox captures skipped as duplicates while
// relocate_duplicates is off (public read, mirrors fetchQuarantine).
export async function fetchDuplicates(
  fetchFn: typeof fetch = fetch,
): Promise<DuplicateItem[]> {
  const resp = await fetchFn('/api/duplicates')
  if (!resp.ok) throw new Error(`duplicates fetch failed: ${resp.status}`)
  return ((await resp.json()) as { items: DuplicateItem[] }).items
}

// Result of POST /api/duplicates/dismiss: rows moved to _duplicates/ and deleted.
// Unknown ids are skipped silently (counted), per-row move errors come back in
// failures so the panel can surface them.
export interface DismissResult {
  dismissed: number
  skipped: number
  failures: { id: number; error: string }[]
}

// Dismiss duplicate rows (admin-guarded: thread authFetch). Synchronous on the
// server (renames are cheap — no job to poll). 409 while a destructive job runs;
// the thrown Error carries the server's explanation when the body provides one
// (FastAPI `detail` — either a bare string or `{message, ...}`), so the panel's
// error line reads "a destructive job is already running" instead of just "409".
export async function dismissDuplicates(
  fetchFn: typeof fetch,
  ids: number[],
): Promise<DismissResult> {
  const resp = await fetchFn('/api/duplicates/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
  if (!resp.ok) {
    let detail: string | null = null
    try {
      const body = (await resp.json()) as { detail?: string | { message?: unknown } }
      const d = body?.detail
      if (typeof d === 'string') detail = d
      else if (d && typeof d.message === 'string') detail = d.message
    } catch {
      // no JSON body (or none at all) — fall back to the status-only message
    }
    throw new Error(`duplicates dismiss failed: ${resp.status}${detail ? ` — ${detail}` : ''}`)
  }
  return (await resp.json()) as DismissResult
}

// Toggle a capture's favorite flag (admin-guarded: thread authFetch). Idempotent
// server-side (keyed by content hash, so it survives undo/re-import).
export async function setFavorite(
  fetchFn: typeof fetch,
  id: number,
  favorite: boolean,
): Promise<{ file_id: number; favorite: boolean }> {
  const resp = await fetchFn('/api/favorite', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_id: id, favorite }),
  })
  if (!resp.ok) throw new Error(`favorite update failed: ${resp.status}`)
  return (await resp.json()) as { file_id: number; favorite: boolean }
}

// Offline forward place-name search: resolve a place/feature name to ranked
// coordinate matches. A blank query short-circuits to [] (no request).
export async function placeSearch(
  query: string,
  fetchFn: typeof fetch = fetch,
): Promise<PlaceResult[]> {
  if (!query.trim()) return []
  const resp = await fetchFn(`/api/place-search?q=${encodeURIComponent(query)}`)
  if (!resp.ok) throw new Error(`place search failed: ${resp.status}`)
  return ((await resp.json()) as { results: PlaceResult[] }).results
}
