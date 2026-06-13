import { describe, it, expect, vi } from 'vitest'
import { runStitchAll } from './stitchAllJob'
import type { StitchState } from './stitchJob'

const noFetch = (() => {
  throw new Error('fetch should not be called — runStitchFn is injected')
}) as unknown as typeof fetch

const ok = (id: number): StitchState => ({ state: 'done', status: 'ok', file_id: id, error: null })
const failed = (id: number): StitchState => ({ state: 'done', status: 'failed', file_id: id, error: 'x' })
const unavailable = (id: number): StitchState => ({ state: 'done', status: 'unavailable', file_id: id, error: null })

describe('runStitchAll', () => {
  it('runs every id in order and counts completions', async () => {
    const order: number[] = []
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) => {
      order.push(id)
      return ok(id)
    })
    const summary = await runStitchAll(noFetch, [1, 2, 3], { runStitchFn })
    expect(order).toEqual([1, 2, 3])
    expect(summary).toEqual({ completed: 3, failed: 0, cancelled: false, failedIds: [] })
  })

  it('stops early (after the in-flight one) when shouldContinue turns false', async () => {
    const order: number[] = []
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) => {
      order.push(id)
      return ok(id)
    })
    let calls = 0
    const shouldContinue = () => {
      calls += 1
      return calls <= 2 // allow the first two starts, refuse the third
    }
    const summary = await runStitchAll(noFetch, [1, 2, 3, 4], { runStitchFn, shouldContinue })
    expect(order).toEqual([1, 2])
    expect(summary.cancelled).toBe(true)
    expect(summary.completed).toBe(2)
  })

  it('counts a failed id without aborting the rest', async () => {
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) =>
      id === 2 ? failed(id) : ok(id),
    )
    const summary = await runStitchAll(noFetch, [1, 2, 3], { runStitchFn })
    expect(summary).toEqual({ completed: 2, failed: 1, cancelled: false, failedIds: [2] })
  })

  it('counts a thrown stitch as failed and continues', async () => {
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) => {
      if (id === 2) throw new Error('boom')
      return ok(id)
    })
    const summary = await runStitchAll(noFetch, [1, 2, 3], { runStitchFn })
    expect(summary).toEqual({ completed: 2, failed: 1, cancelled: false, failedIds: [2] })
  })

  it('does not count an unavailable stitch (Hugin absent) as failed', async () => {
    // 'unavailable' leaves stitch_status NULL (no failed badge) — it is not a failure
    // of that panorama, so it must be excluded from failedIds/failed.
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) =>
      id === 2 ? unavailable(id) : ok(id),
    )
    const summary = await runStitchAll(noFetch, [1, 2, 3], { runStitchFn })
    expect(summary).toEqual({ completed: 2, failed: 0, cancelled: false, failedIds: [] })
  })

  it('collects every failed id (status-failed and thrown) in order', async () => {
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) => {
      if (id === 2) return failed(id) // status !== 'ok'
      if (id === 4) throw new Error('boom') // thrown start error
      return ok(id)
    })
    const summary = await runStitchAll(noFetch, [1, 2, 3, 4], { runStitchFn })
    expect(summary.failedIds).toEqual([2, 4])
    expect(summary.failed).toBe(2)
  })

  it('reports progress per id', async () => {
    const runStitchFn = vi.fn(async (_f: typeof fetch, id: number) => ok(id))
    const seen: Array<{ done: number; current: number | null }> = []
    await runStitchAll(noFetch, [7, 8], {
      runStitchFn,
      onProgress: (p) => seen.push({ done: p.done, current: p.current }),
    })
    // before id 7, after id 7 (=before+1), before id 8, after id 8
    expect(seen).toEqual([
      { done: 0, current: 7 },
      { done: 1, current: null },
      { done: 1, current: 8 },
      { done: 2, current: null },
    ])
  })
})
