import { describe, it, expect, vi } from 'vitest'
import {
  fmtSize,
  runRepairFix,
  runRepairScan,
  type RepairRunState,
  type RepairScanState,
} from './repairJob'

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response
}

const scanBase: Omit<RepairScanState, 'state'> = {
  checked: 0, ok: 0, broken: 0, processed: 0, current: null,
  untrunc_available: false, items: [], error: null,
}

const runBase: Omit<RepairRunState, 'state'> = {
  file_id: 7, reference_id: 9, phase: '', bytes_done: 0, bytes_total: 0,
  status: '', warning: null, fixed_path: null, codec: null, width: null,
  height: null, duration_s: null, size: null, error: null, output_tail: [],
}

describe('runRepairScan', () => {
  it('posts then polls to a terminal state, reporting progress', async () => {
    const statuses: RepairScanState[] = [
      { ...scanBase, state: 'running', processed: 2, current: 'DJI_0002.MP4' },
      {
        ...scanBase, state: 'done', checked: 3, ok: 2, broken: 1,
        untrunc_available: true,
        items: [{
          id: 5, filename: 'DJI_0002.MP4', media_type: 'video',
          date: '2023-07-05', size: 0, status: 'zero-byte', error: null,
          path: '_no-gps/2023-07-05/DJI_0002.MP4', hidden_from_no_gps: false,
        }],
      },
    ]
    let poll = 0
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(url).toBe('/api/repair/scan')
        return ok({ job_id: 'scan1' })
      }
      expect(url).toBe('/api/repair/scan/status/scan1')
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const seen: string[] = []
    const final = await runRepairScan(fetchFn, {
      onProgress: (s) => seen.push(s.state),
      intervalMs: 1,
    })
    expect(final.state).toBe('done')
    expect(final.broken).toBe(1)
    expect(final.items[0].status).toBe('zero-byte')
    expect(seen).toEqual(['running', 'done'])
  })

  it('throws if the start request fails', async () => {
    const fetchFn = (async () => ({ ok: false, status: 401 }) as Response) as unknown as typeof fetch
    await expect(runRepairScan(fetchFn)).rejects.toThrow(/repair scan start failed: 401/)
  })
})

describe('runRepairFix', () => {
  it('posts the file + reference ids and polls phases to done', async () => {
    const statuses: RepairRunState[] = [
      { ...runBase, state: 'running', phase: 'backup', bytes_done: 10, bytes_total: 100 },
      { ...runBase, state: 'running', phase: 'repair', bytes_done: 60, bytes_total: 100 },
      {
        ...runBase, state: 'done', status: 'ok', phase: 'verify',
        fixed_path: '_repair/fixed/7_DJI_0771.MP4', codec: 'h264', size: 90,
      },
    ]
    let poll = 0
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(url).toBe('/api/repair/run')
        expect(JSON.parse(String(init.body))).toEqual({ file_id: 7, reference_id: 9 })
        return ok({ job_id: 'fix1' })
      }
      expect(url).toBe('/api/repair/status/fix1')
      return ok(statuses[Math.min(poll++, statuses.length - 1)])
    }) as unknown as typeof fetch

    const phases: string[] = []
    const final = await runRepairFix(fetchFn, 7, 9, {
      onProgress: (s) => phases.push(s.phase),
      intervalMs: 1,
    })
    expect(final.status).toBe('ok')
    expect(final.fixed_path).toBe('_repair/fixed/7_DJI_0771.MP4')
    expect(phases).toEqual(['backup', 'repair', 'verify'])
  })

  it('surfaces a failed rebuild as a terminal done state', async () => {
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return ok({ job_id: 'fix2' })
      return ok({
        ...runBase, state: 'done', status: 'failed',
        error: 'untrunc produced no output (exit 2)', output_tail: ['boom'],
      })
    }) as unknown as typeof fetch
    const final = await runRepairFix(fetchFn, 7, 9, { intervalMs: 1 })
    expect(final.status).toBe('failed')
    expect(final.error).toMatch(/no output/)
  })
})

describe('fmtSize', () => {
  it('formats across magnitudes', () => {
    expect(fmtSize(0)).toBe('0 B')
    expect(fmtSize(513816795)).toBe('490 MB')
    expect(fmtSize(3775933767)).toBe('3.52 GB')
  })
})
