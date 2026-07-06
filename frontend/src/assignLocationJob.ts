// Pure driver for the bulk assign-location job: POST /api/assign-location
// {file_ids,lat,lon} then poll /api/assign-location/status/{id} to a terminal
// state. Mirrors retagJob.ts; `fetchFn` is injectable for tests. Promotes one or
// more no-GPS (quarantined) captures to organized at a user-picked coordinate.

import { pollJob } from './pollJob'

export interface AssignState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  assigned: number
  skipped: number
  place_string: string | null
  total: number // selected captures (set at submit) — the progress denominator
  processed: number
  current: string | null
  error: string | null
  failures: string[]
}

export async function runAssignLocation(
  fetchFn: typeof fetch,
  fileIds: number[],
  lat: number,
  lon: number,
  opts: { onProgress?: (s: AssignState) => void; intervalMs?: number } = {},
): Promise<AssignState> {
  // Bounded by the selection size (each file is one re-file); an hour is generous.
  return pollJob<AssignState>(fetchFn, {
    kind: 'assign',
    startUrl: '/api/assign-location',
    startInit: {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_ids: fileIds, lat, lon }),
    },
    statusUrl: (id) => `/api/assign-location/status/${id}`,
    terminal: new Set(['done', 'error']),
  }, { timeoutMs: 60 * 60_000, ...opts })
}
