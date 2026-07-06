// Pure driver for the B8 background undo job: POST /api/undo then poll
// /api/undo/status/{id} to a terminal state. `fetchFn` is injectable for tests.

import { pollJob } from './pollJob'

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

export async function runUndo(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: UndoState) => void; intervalMs?: number } = {},
): Promise<UndoState> {
  return pollJob<UndoState>(fetchFn, {
    kind: 'undo',
    startUrl: '/api/undo',
    statusUrl: (id) => `/api/undo/status/${id}`,
  }, opts)
}
