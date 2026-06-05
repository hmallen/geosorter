// Pure driver for the B13 background panorama-stitch job: POST /api/stitch/{id}
// then poll /api/stitch/status/{job_id} to a terminal state. `fetchFn` is
// injectable. A stitch is ~7 min, so polling is deliberately slow by default.

export interface StitchState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  // '' (in progress) | 'ok' (hero ready) | 'failed' (degenerate/error, use gallery)
  // | 'unavailable' (Hugin not installed)
  status: string
  file_id: number | null
  // Live Hugin pipeline progress: which of the six steps is currently running.
  step?: number
  step_total?: number
  step_name?: string
  error: string | null
}

const TERMINAL = new Set(['done', 'error'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runStitch(
  fetchFn: typeof fetch,
  fileId: number,
  opts: { onProgress?: (s: StitchState) => void; intervalMs?: number } = {},
): Promise<StitchState> {
  const { onProgress, intervalMs = 2000 } = opts
  const started = await fetchFn(`/api/stitch/${fileId}`, { method: 'POST' })
  if (!started.ok) throw new Error(`stitch start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/stitch/status/${job_id}`)
    if (!resp.ok) throw new Error(`stitch status failed: ${resp.status}`)
    const state = (await resp.json()) as StitchState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
