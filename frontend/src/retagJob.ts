// Pure driver for the B8 background re-tag job: POST /api/retag {file_id,lat,lon}
// then poll /api/retag/status/{id} to a terminal state. `fetchFn` is injectable.

import { pollJob } from './pollJob'

export interface RetagState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  status: string // RetagReport status: 'retagged' | 'not_found' | 'failed' | ''
  moved: number
  place_string: string | null
  processed: number
  current: string | null
  error: string | null
}

export async function runRetag(
  fetchFn: typeof fetch,
  fileId: number,
  lat: number,
  lon: number,
  opts: { onProgress?: (s: RetagState) => void; intervalMs?: number } = {},
): Promise<RetagState> {
  // A single-file re-file is minutes at worst; a wedged job should not poll forever.
  return pollJob<RetagState>(fetchFn, {
    kind: 'retag',
    startUrl: '/api/retag',
    startInit: {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: fileId, lat, lon }),
    },
    statusUrl: (id) => `/api/retag/status/${id}`,
    terminal: new Set(['done', 'error']),
  }, { timeoutMs: 10 * 60_000, ...opts })
}
