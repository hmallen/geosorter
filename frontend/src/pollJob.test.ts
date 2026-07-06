import { describe, it, expect, vi } from 'vitest'
import { pollJob, type JobSpec } from './pollJob'

interface FakeState {
  state: string
  n?: number
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const SPEC: JobSpec = {
  kind: 'fake',
  startUrl: '/api/fake',
  statusUrl: (id) => `/api/fake/status/${id}`,
}

describe('pollJob', () => {
  it('posts the start request then polls to a terminal state', async () => {
    const statuses: FakeState[] = [
      { state: 'running', n: 1 },
      { state: 'done', n: 2 },
    ]
    let poll = 0
    const urls: string[] = []
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(url)
      if (init?.method === 'POST') return ok({ job_id: 'j1' })
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await pollJob<FakeState>(fetchFn, SPEC, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })

    expect(final).toEqual({ state: 'done', n: 2 })
    expect(seen).toEqual(['running', 'done'])
    expect(urls[0]).toBe('/api/fake')
    expect(urls[1]).toBe('/api/fake/status/j1')
  })

  it('treats cancelled as terminal by default', async () => {
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? ok({ job_id: 'j' }) : ok({ state: 'cancelled' })
    ) as unknown as typeof fetch
    const final = await pollJob<FakeState>(fetchFn, SPEC, { intervalMs: 1 })
    expect(final.state).toBe('cancelled')
  })

  it('honours a custom terminal set', async () => {
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? ok({ job_id: 'j' }) : ok({ state: 'weird' })
    ) as unknown as typeof fetch
    const final = await pollJob<FakeState>(fetchFn, { ...SPEC, terminal: new Set(['weird']) })
    expect(final.state).toBe('weird')
  })

  it('throws when the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 409 }) as Response) as unknown as typeof fetch
    await expect(pollJob<FakeState>(fetchFn, SPEC)).rejects.toThrow(/fake start failed: 409/)
  })

  it('throws when a status poll fails', async () => {
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? ok({ job_id: 'j' })
        : ({ ok: false, status: 404 } as Response)
    ) as unknown as typeof fetch
    await expect(pollJob<FakeState>(fetchFn, SPEC)).rejects.toThrow(/fake status failed: 404/)
  })

  it('rejects after timeoutMs when the job never reaches a terminal state', async () => {
    // A backend job wedged in 'running' must not poll forever.
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? ok({ job_id: 'j' }) : ok({ state: 'running' })
    ) as unknown as typeof fetch
    await expect(
      pollJob<FakeState>(fetchFn, SPEC, { intervalMs: 1, timeoutMs: 20 }),
    ).rejects.toThrow(/fake timed out/)
  })

  it('reports late progress before timing out', async () => {
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? ok({ job_id: 'j' }) : ok({ state: 'running' })
    ) as unknown as typeof fetch
    const seen: string[] = []
    await pollJob<FakeState>(fetchFn, SPEC, {
      intervalMs: 1,
      timeoutMs: 20,
      onProgress: (s) => seen.push(s.state),
    }).catch(() => undefined)
    expect(seen.length).toBeGreaterThan(0)
    expect(seen.every((s) => s === 'running')).toBe(true)
  })
})
