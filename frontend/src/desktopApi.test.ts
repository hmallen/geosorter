import { describe, expect, it, vi, afterEach } from 'vitest'
import { desktopRequest, progressText, type DesktopJob } from './desktopApi'

afterEach(() => vi.unstubAllGlobals())
describe('desktop setup progress', () => {
  const job: DesktopJob = { job_id: '1', kind: 'cities', state: 'running', phase: 'indexing', current: '', done: 0, total: 0, message: null }
  it('does not invent a percentage without a known total', () => {
    expect(progressText(job)).toBe('Building the place index')
    expect(progressText({ ...job, phase: 'downloading', done: 25, total: 100 })).toContain('25%')
  })
  it('shows recovery guidance after interruption', () => {
    expect(progressText({ ...job, state: 'interrupted', message: 'Retry from saved downloads.' })).toBe('Retry from saved downloads.')
  })
  it('surfaces actionable API errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Connect the drive.' }), { status: 400 })))
    await expect(desktopRequest('retry', {})).rejects.toThrow('Connect the drive.')
  })
})
