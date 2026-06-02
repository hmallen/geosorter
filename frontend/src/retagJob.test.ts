import { describe, it, expect, vi } from 'vitest'
import { runRetag, type RetagState } from './retagJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  status: '', moved: 0, place_string: null, processed: 0, current: null, error: null,
}

describe('runRetag', () => {
  it('posts the coordinate then polls to a terminal state, reporting progress', async () => {
    const statuses: RetagState[] = [
      { ...base, state: 'running', processed: 1, current: 'x.JPG' },
      { ...base, state: 'done', status: 'retagged', moved: 2, place_string: 'Denver, Colorado, United States', processed: 1 },
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
    const final = await runRetag(fetchFn, 7, 39.7, -104.9, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })

    expect(JSON.parse(postedBody as string)).toEqual({ file_id: 7, lat: 39.7, lon: -104.9 })
    expect(final.state).toBe('done')
    expect(final.status).toBe('retagged')
    expect(final.moved).toBe(2)
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runRetag(fetchFn, 1, 0, 0)).rejects.toThrow(/retag start failed: 500/)
  })
})
