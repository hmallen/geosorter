// Typed helpers for the B6 HTTP API: media-URL builders + the library fetch.
import type { LibraryFC } from './types'

// Encode each path segment but preserve the separators, so a library-relative
// path like "Boulder, Colorado/2024-07-04/x.JPG" becomes a valid URL the
// backend's {relpath:path} route decodes back to the same path.
const enc = (path: string): string => path.split('/').map(encodeURIComponent).join('/')

export const mediaUrl = (path: string): string => `/api/media/${enc(path)}`
export const thumbUrl = (path: string): string => `/api/thumb/${enc(path)}`
export const previewUrl = (path: string): string => `/api/preview/${enc(path)}`
export const posterUrl = (path: string): string => `/api/poster/${enc(path)}`
export const videoUrl = (path: string): string => `/api/video/${enc(path)}`

export async function fetchLibrary(fetchFn: typeof fetch = fetch): Promise<LibraryFC> {
  const resp = await fetchFn('/api/library')
  if (!resp.ok) throw new Error(`library fetch failed: ${resp.status}`)
  return (await resp.json()) as LibraryFC
}

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
