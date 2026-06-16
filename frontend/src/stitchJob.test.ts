import { describe, it, expect, vi } from 'vitest'
import { runStitch, type StitchState } from './stitchJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = { file_id: 7, status: '', error: null }

describe('runStitch', () => {
  it('POSTs to start the stitch, then polls status to a terminal state', async () => {
    const statuses: StitchState[] = [
      { ...base, state: 'running' },
      { ...base, state: 'done', status: 'ok' },
    ]
    let poll = 0
    let posted = false
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posted = true
        expect(url).toBe('/api/stitch/7')
        return ok({ job_id: 'job1' })
      }
      expect(url).toBe('/api/stitch/status/job1')
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runStitch(fetchFn, 7, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })

    expect(posted).toBe(true)
    expect(final.state).toBe('done')
    expect(final.status).toBe('ok')
    expect(seen).toEqual(['running', 'done'])
  })

  it('surfaces an unavailable terminal state (Hugin absent)', async () => {
    const fetchFn = (async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? ok({ job_id: 'j' })
        : ok({ ...base, state: 'done', status: 'unavailable' })) as unknown as typeof fetch
    const final = await runStitch(fetchFn, 7, { intervalMs: 1 })
    expect(final.state).toBe('done')
    expect(final.status).toBe('unavailable')
  })

  it('sends a {force, projection} body for a manual re-stitch', async () => {
    let postInit: RequestInit | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        postInit = init
        return ok({ job_id: 'j' })
      }
      return ok({ ...base, state: 'done', status: 'ok' })
    }) as unknown as typeof fetch

    await runStitch(fetchFn, 7, { intervalMs: 1, force: true, projection: 'cylindrical' })
    expect(JSON.parse(postInit?.body as string)).toEqual({
      force: true, projection: 'cylindrical',
    })
    expect((postInit?.headers as Record<string, string>)['Content-Type']).toBe(
      'application/json',
    )
  })

  it('sends a bare POST (no body) when neither force nor projection is given', async () => {
    let postInit: RequestInit | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        postInit = init
        return ok({ job_id: 'j' })
      }
      return ok({ ...base, state: 'done', status: 'ok' })
    }) as unknown as typeof fetch

    await runStitch(fetchFn, 7, { intervalMs: 1 })
    expect(postInit?.body).toBeUndefined()
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    await expect(runStitch(fetchFn, 1)).rejects.toThrow(/stitch start failed: 500/)
  })
})
