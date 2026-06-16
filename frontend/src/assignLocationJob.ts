// Pure driver for the bulk assign-location job: POST /api/assign-location
// {file_ids,lat,lon} then poll /api/assign-location/status/{id} to a terminal
// state. Mirrors retagJob.ts; `fetchFn` is injectable for tests. Promotes one or
// more no-GPS (quarantined) captures to organized at a user-picked coordinate.

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

const TERMINAL = new Set(['done', 'error'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runAssignLocation(
  fetchFn: typeof fetch,
  fileIds: number[],
  lat: number,
  lon: number,
  opts: { onProgress?: (s: AssignState) => void; intervalMs?: number } = {},
): Promise<AssignState> {
  const { onProgress, intervalMs = 500 } = opts
  const started = await fetchFn('/api/assign-location', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_ids: fileIds, lat, lon }),
  })
  if (!started.ok) throw new Error(`assign start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/assign-location/status/${job_id}`)
    if (!resp.ok) throw new Error(`assign status failed: ${resp.status}`)
    const state = (await resp.json()) as AssignState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
