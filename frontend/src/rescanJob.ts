// Pure driver for the background rescan job: POST /api/rescan then poll
// /api/rescan/status/{id} to a terminal state. `fetchFn` is injectable for tests.
// Rescan reconciles the index with on-disk state, pruning rows for files that
// have left the library.

import { pollJob } from './pollJob'

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

export async function runRescan(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: RescanState) => void; intervalMs?: number } = {},
): Promise<RescanState> {
  // An index-vs-disk pass is bounded by library size (minutes, not hours).
  return pollJob<RescanState>(fetchFn, {
    kind: 'rescan',
    startUrl: '/api/rescan',
    statusUrl: (id) => `/api/rescan/status/${id}`,
    terminal: new Set(['done', 'error']),
  }, { timeoutMs: 30 * 60_000, ...opts })
}
