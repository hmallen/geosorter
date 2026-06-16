import { describe, it, expect, vi } from 'vitest'
import { runAssignLocation, type AssignState } from './assignLocationJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  assigned: 0, skipped: 0, place_string: null, total: 0, processed: 0, current: null,
  error: null, failures: [] as string[],
}

describe('runAssignLocation', () => {
  it('posts the ids + coordinate then polls to a terminal state, reporting progress', async () => {
    const statuses: AssignState[] = [
      { ...base, state: 'running', total: 2, processed: 1, current: 'q.JPG' },
      { ...base, state: 'done', total: 2, assigned: 2, skipped: 1, place_string: 'Moab, Utah, United States', processed: 2 },
    ]
    let poll = 0
    let postedBody: string | undefined
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        postedBody = init.body as string
        return ok({ job_id: 'job1' })
      }
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runAssignLocation(fetchFn, [3, 4], 38.57, -109.55, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })

    expect(JSON.parse(postedBody as string)).toEqual({
      file_ids: [3, 4], lat: 38.57, lon: -109.55,
    })
    expect(final.state).toBe('done')
    expect(final.assigned).toBe(2)
    expect(final.skipped).toBe(1)
    expect(final.total).toBe(2)
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runAssignLocation(fetchFn, [1], 0, 0)).rejects.toThrow(
      /assign start failed: 500/,
    )
  })
})
