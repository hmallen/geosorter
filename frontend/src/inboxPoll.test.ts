import { describe, it, expect, vi } from 'vitest'
import { createInboxPoll } from './inboxPoll'
import type { InboxCount } from './api'

const COUNT: InboxCount = { files: 2, captures: 1 }

// Drain the microtask queue (the driver's .then -> .catch -> .finally chain spans
// several ticks) by bouncing through a macrotask.
const flush = () => new Promise((r) => setTimeout(r, 0))

// A manually-resolvable promise so a fetch can be held "in flight" across calls.
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('createInboxPoll', () => {
  it('drops a refresh while one fetch is already in flight', async () => {
    const d = deferred<InboxCount>()
    const fetchFn = vi.fn(() => d.promise)
    const onCount = vi.fn()
    const refresh = createInboxPoll(fetchFn, onCount)

    refresh()
    refresh() // in flight -> must be dropped
    expect(fetchFn).toHaveBeenCalledTimes(1)

    d.resolve(COUNT)
    await flush()
    expect(onCount).toHaveBeenCalledWith(COUNT)
  })

  it('fetches again after the in-flight fetch resolves', async () => {
    const first = deferred<InboxCount>()
    const second = deferred<InboxCount>()
    const fetchFn = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const onCount = vi.fn()
    const refresh = createInboxPoll(fetchFn, onCount)

    refresh()
    first.resolve(COUNT)
    await flush()

    refresh() // guard cleared -> fetches again
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('resets the guard when a fetch rejects', async () => {
    const first = deferred<InboxCount>()
    const second = deferred<InboxCount>()
    const fetchFn = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const onError = vi.fn()
    const refresh = createInboxPoll(fetchFn, () => {}, onError)

    refresh()
    first.reject(new Error('boom'))
    await flush()
    expect(onError).toHaveBeenCalled()

    refresh() // guard reset after the error -> fetches again
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('resets the guard when fetchFn throws synchronously', () => {
    let calls = 0
    const fetchFn = vi.fn((): Promise<InboxCount> => {
      calls += 1
      if (calls === 1) throw new Error('sync boom')
      return Promise.resolve(COUNT)
    })
    const onError = vi.fn()
    const refresh = createInboxPoll(fetchFn, () => {}, onError)

    refresh() // throws synchronously -> guard must reset, not wedge
    expect(onError).toHaveBeenCalled()

    refresh() // guard reset -> fetches again
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })
})
