import { describe, it, expect, vi } from 'vitest'
import { runRescan, type RescanState } from './rescanJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  checked: 0, pruned: 0, kept: 0, processed: 0, current: null,
  warnings: [], orphaned: [], error: null,
}

describe('runRescan', () => {
  it('posts then polls to a terminal state, reporting progress', async () => {
    const statuses: RescanState[] = [
      { ...base, state: 'running', processed: 1, current: 'a.jpg' },
      { ...base, state: 'done', checked: 3, kept: 2, pruned: 1, processed: 3 },
    ]
    let poll = 0
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(url).toBe('/api/rescan')
        return ok({ job_id: 'job1' })
      }
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runRescan(fetchFn, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })

    expect(final.state).toBe('done')
    expect(final.pruned).toBe(1)
    expect(final.kept).toBe(2)
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runRescan(fetchFn)).rejects.toThrow(/rescan start failed: 500/)
  })
})
