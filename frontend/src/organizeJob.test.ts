import { describe, it, expect, vi } from 'vitest'
import {
  runOrganize,
  progressLabel,
  loadProgressLabel,
  resultLabel,
  type JobState,
} from './organizeJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const base = {
  organized: 0, quarantined: 0, duplicates_skipped: 0, companions: 0,
  processed: 0, current: null, current_phase: null, bytes_done: 0, bytes_total: 0,
  failures: [], error: null,
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

  it('posts a {primaries} body for a partial import', async () => {
    let postInit: RequestInit | undefined
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        postInit = init
        return ok({ job_id: 'job1' })
      }
      return ok({ ...base, state: 'done' })
    }) as unknown as typeof fetch

    await runOrganize(fetchFn, { intervalMs: 1 }, ['a', 'b'])
    expect(postInit?.body).toBe(JSON.stringify({ primaries: ['a', 'b'] }))
    expect((postInit?.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('posts no body for a full import (select-all default)', async () => {
    let postInit: RequestInit | undefined
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        postInit = init
        return ok({ job_id: 'job1' })
      }
      return ok({ ...base, state: 'done' })
    }) as unknown as typeof fetch

    await runOrganize(fetchFn, { intervalMs: 1 })
    expect(postInit?.body).toBeUndefined()
  })
})

describe('resultLabel', () => {
  it('appends the error detail when the run errored', () => {
    const job: JobState = {
      ...base, state: 'error', organized: 0, quarantined: 0,
      failures: [String.raw`Z:\inbox\DJI_0003.MP4: network name no longer available`],
      error: String.raw`Z:\inbox\DJI_0003.MP4: network name no longer available`,
    }
    const label = resultLabel(job)
    expect(label).toContain('error: organized 0, quarantined 0')
    expect(label).toContain('errors 1')
    expect(label).toContain('network name no longer available') // the actual detail is shown
  })

  it('omits error detail on a clean done run', () => {
    const job: JobState = { ...base, state: 'done', organized: 2, quarantined: 1 }
    const label = resultLabel(job)
    expect(label).toBe('done: organized 2, quarantined 1')
  })
})

describe('loadProgressLabel', () => {
  it('reports loaded / total / remaining captures', () => {
    expect(loadProgressLabel(2, 5)).toBe('loaded 2 of 5 captures (3 remaining)')
  })
  it('singularizes a one-capture import', () => {
    expect(loadProgressLabel(0, 1)).toBe('loaded 0 of 1 capture (1 remaining)')
  })
  it('clamps when processed overshoots total (skips/dups)', () => {
    expect(loadProgressLabel(7, 5)).toBe('loaded 5 of 5 captures (0 remaining)')
  })
})

describe('progressLabel', () => {
  it('shows phase, bytes and percent while copying', () => {
    const job: JobState = {
      ...base, state: 'running', current: 'DJI_0003.MP4',
      current_phase: 'copying', bytes_done: 512 * 1024 * 1024, bytes_total: 1024 * 1024 * 1024,
    }
    const label = progressLabel(job)
    expect(label).toContain('copying')
    expect(label).toContain('DJI_0003.MP4')
    expect(label).toContain('(50%)')
  })

  it('falls back to the file counter when no byte total is known', () => {
    const job: JobState = { ...base, state: 'running', processed: 4, current: 'a.jpg' }
    expect(progressLabel(job)).toBe('processing 4 — a.jpg')
  })
})
