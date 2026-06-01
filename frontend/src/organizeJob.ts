// Pure driver for the B6 background organize job: POST /api/organize then poll
// /api/organize/status/{id} to a terminal state. `fetchFn` is injectable for tests.

export interface JobState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  organized: number
  quarantined: number
  duplicates_skipped: number
  companions: number
  processed: number
  current: string | null
  failures: string[]
  error: string | null
}

const TERMINAL = new Set(['done', 'error', 'cancelled'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runOrganize(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: JobState) => void; intervalMs?: number } = {},
): Promise<JobState> {
  const { onProgress, intervalMs = 500 } = opts
  const started = await fetchFn('/api/organize', { method: 'POST' })
  if (!started.ok) throw new Error(`organize start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/organize/status/${job_id}`)
    if (!resp.ok) throw new Error(`organize status failed: ${resp.status}`)
    const state = (await resp.json()) as JobState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
