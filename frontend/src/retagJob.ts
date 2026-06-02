// Pure driver for the B8 background re-tag job: POST /api/retag {file_id,lat,lon}
// then poll /api/retag/status/{id} to a terminal state. `fetchFn` is injectable.

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

const TERMINAL = new Set(['done', 'error'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runRetag(
  fetchFn: typeof fetch,
  fileId: number,
  lat: number,
  lon: number,
  opts: { onProgress?: (s: RetagState) => void; intervalMs?: number } = {},
): Promise<RetagState> {
  const { onProgress, intervalMs = 500 } = opts
  const started = await fetchFn('/api/retag', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_id: fileId, lat, lon }),
  })
  if (!started.ok) throw new Error(`retag start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/retag/status/${job_id}`)
    if (!resp.ok) throw new Error(`retag status failed: ${resp.status}`)
    const state = (await resp.json()) as RetagState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
