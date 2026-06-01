import { describe, it, expect, vi } from 'vitest'
import { runOrganize, type JobState } from './organizeJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  organized: 0, quarantined: 0, duplicates_skipped: 0, companions: 0,
  processed: 0, current: null, failures: [], error: null,
}

describe('runOrganize', () => {
  it('posts then polls to a terminal state, reporting progress', async () => {
    const statuses: JobState[] = [
      { ...base, state: 'running', processed: 1, current: 'a.jpg' },
      { ...base, state: 'done', organized: 2, quarantined: 1, processed: 3 },
    ]
    let poll = 0
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return ok({ job_id: 'job1' })
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runOrganize(fetchFn, { onProgress: (s) => seen.push(s.state), intervalMs: 1 })

    expect(final.state).toBe('done')
    expect(final.organized).toBe(2)
    expect(final.quarantined).toBe(1)
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runOrganize(fetchFn)).rejects.toThrow(/organize start failed: 500/)
  })
})
