// Pure driver for the background rescan job: POST /api/rescan then poll
// /api/rescan/status/{id} to a terminal state. `fetchFn` is injectable for tests.
// Rescan reconciles the index with on-disk state, pruning rows for files that
// have left the library.

export interface RescanState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  checked: number
  pruned: number
  kept: number
  processed: number
  current: string | null
  error: string | null
  warnings: string[]
  orphaned: string[]
}

const TERMINAL = new Set(['done', 'error'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runRescan(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: RescanState) => void; intervalMs?: number } = {},
): Promise<RescanState> {
  const { onProgress, intervalMs = 500 } = opts
  const started = await fetchFn('/api/rescan', { method: 'POST' })
  if (!started.ok) throw new Error(`rescan start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/rescan/status/${job_id}`)
    if (!resp.ok) throw new Error(`rescan status failed: ${resp.status}`)
    const state = (await resp.json()) as RescanState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
