import { describe, it, expect, vi } from 'vitest'
import { runUndo, type UndoState } from './undoJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  batch_id: null, restored: 0, missing: 0, processed: 0, current: null,
  nothing_to_undo: false, conflicts: [], failures: [], error: null,
}

describe('runUndo', () => {
  it('posts then polls to a terminal state, reporting progress', async () => {
    const statuses: UndoState[] = [
      { ...base, state: 'running', processed: 1, current: 'a.jpg' },
      { ...base, state: 'done', batch_id: 'b1', restored: 2, processed: 2 },
    ]
    let poll = 0
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return ok({ job_id: 'job1' })
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runUndo(fetchFn, { onProgress: (s) => seen.push(s.state), intervalMs: 1 })

    expect(final.state).toBe('done')
    expect(final.restored).toBe(2)
    expect(final.batch_id).toBe('b1')
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runUndo(fetchFn)).rejects.toThrow(/undo start failed: 500/)
  })
})
