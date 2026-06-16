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
  // Detected projection of a successful hero (m-fix-panorama-projection-autodetect):
  // 'equirectangular' | 'flat' | '' while in progress. Lets the lightbox pick its
  // viewer immediately on completion, before the library reload.
  projection?: string
  error: string | null
}

const TERMINAL = new Set(['done', 'error'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runStitch(
  fetchFn: typeof fetch,
  fileId: number,
  opts: {
    onProgress?: (s: StitchState) => void
    intervalMs?: number
    // Manual re-stitch (m-implement-ui-...-restitch): force a cold re-run and/or
    // override the auto-detected projection. Omitted -> a bare POST (first stitch).
    force?: boolean
    projection?: string
  } = {},
): Promise<StitchState> {
  const { onProgress, intervalMs = 2000, force, projection } = opts
  // Only attach a JSON body when an override is requested, so the first-time stitch
  // stays a bare POST (the backend defaults force=false / auto-detect).
  const post: RequestInit =
    force !== undefined || projection !== undefined
      ? {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force, projection }),
        }
      : { method: 'POST' }
  const started = await fetchFn(`/api/stitch/${fileId}`, post)
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
