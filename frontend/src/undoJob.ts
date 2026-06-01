// Pure driver for the B8 background undo job: POST /api/undo then poll
// /api/undo/status/{id} to a terminal state. `fetchFn` is injectable for tests.

export interface UndoState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  batch_id: string | null
  restored: number
  missing: number
  processed: number
  current: string | null
  nothing_to_undo: boolean
  conflicts: string[]
  failures: string[]
  error: string | null
}

const TERMINAL = new Set(['done', 'error', 'cancelled'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runUndo(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: UndoState) => void; intervalMs?: number } = {},
): Promise<UndoState> {
  const { onProgress, intervalMs = 500 } = opts
  const started = await fetchFn('/api/undo', { method: 'POST' })
  if (!started.ok) throw new Error(`undo start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/undo/status/${job_id}`)
    if (!resp.ok) throw new Error(`undo status failed: ${resp.status}`)
    const state = (await resp.json()) as UndoState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
