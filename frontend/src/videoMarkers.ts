import type { LibraryFeature } from './types'

export interface VideoMarker {
  id: number
  file_id: number
  time_s: number
  note: string
  created_at: string
  updated_at: string
}

export interface MarkerDraft { time_s: number; note: string }

export function parseMarkerTime(text: string): number | null {
  const parts = text.trim().split(':')
  if (parts.length > 3 || !parts.every((p, i) =>
    (i === parts.length - 1 ? /^\d+(?:\.\d+)?$/ : /^\d+$/).test(p))) return null
  const values = parts.map(Number)
  if (values.slice(1).some((v) => v >= 60)) return null
  const seconds = values.reduce((total, v) => total * 60 + v, 0)
  return Number.isFinite(seconds) ? seconds : null
}

export function formatMarkerTime(seconds: number): string {
  const ms = Math.round(Math.max(0, Number.isFinite(seconds) ? seconds : 0) * 1000)
  const h = Math.floor(ms / 3600000)
  const m = Math.floor(ms / 60000) % 60
  const s = ((ms % 60000) / 1000).toFixed(3).padStart(6, '0')
  return `${h ? `${h}:${String(m).padStart(2, '0')}` : m}:${s}`
}

export const sortMarkers = (markers: VideoMarker[]) =>
  [...markers].sort((a, b) => a.time_s - b.time_s || a.id - b.id)

export function groupMarkers(markers: VideoMarker[], features: LibraryFeature[], query: string) {
  const byId = new Map(features.map((f) => [f.properties.id, f]))
  const groups = new Map<number, { file: LibraryFeature; markers: VideoMarker[] }>()
  const needle = query.trim().toLocaleLowerCase()
  for (const marker of markers) {
    const file = byId.get(marker.file_id)
    if (!file || !(file.properties.filename + '\n' + marker.note).toLocaleLowerCase().includes(needle)) continue
    const group = groups.get(marker.file_id) ?? { file, markers: [] }
    group.markers.push(marker)
    groups.set(marker.file_id, group)
  }
  return [...groups.values()].sort((a, b) =>
    (b.file.properties.capture_ts_local ?? b.file.properties.local_date ?? '').localeCompare(
      a.file.properties.capture_ts_local ?? a.file.properties.local_date ?? '') ||
    a.file.properties.id - b.file.properties.id,
  ).map((g) => ({ ...g, markers: sortMarkers(g.markers) }))
}

async function markerResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Marker request failed (${resp.status}). Please retry.`)
  }
  return resp.status === 204 ? undefined as T : resp.json() as Promise<T>
}

export async function fetchVideoMarkers(fetchFn: typeof fetch, fileId?: number) {
  const result = await markerResponse<{ markers: VideoMarker[] }>(await fetchFn(
    `/api/video-markers${fileId === undefined ? '' : `?file_id=${fileId}`}`,
  ))
  return sortMarkers(result.markers)
}

export async function saveVideoMarker(fetchFn: typeof fetch, fileId: number, draft: MarkerDraft, id?: number) {
  return markerResponse<VideoMarker>(await fetchFn(`/api/video-markers${id === undefined ? '' : `/${id}`}`, {
    method: id === undefined ? 'POST' : 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(id === undefined ? { ...draft, file_id: fileId } : draft),
  }))
}

export async function deleteVideoMarker(fetchFn: typeof fetch, id: number) {
  return markerResponse<void>(await fetchFn(`/api/video-markers/${id}`, { method: 'DELETE' }))
}
