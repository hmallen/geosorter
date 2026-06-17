import { describe, it, expect } from 'vitest'
import { makeAuthFetch, login, logout, fetchAuthStatus, isAdmin } from './auth'

describe('isAdmin', () => {
  it('is true when no login is required (open app), regardless of token', () => {
    expect(isAdmin(false, null)).toBe(true)
    expect(isAdmin(false, 'tok')).toBe(true)
  })

  it('requires a token when login is required', () => {
    expect(isAdmin(true, null)).toBe(false)
    expect(isAdmin(true, 'tok')).toBe(true)
  })
})

describe('makeAuthFetch', () => {
  it('adds the Authorization header when a token is present', async () => {
    let seen: Headers | undefined
    const base = (async (_url: string, init?: RequestInit) => {
      seen = new Headers(init?.headers)
      return { ok: true, status: 200 } as Response
    }) as unknown as typeof fetch
    const f = makeAuthFetch('tok123', base)
    await f('/api/rescan', { method: 'POST' })
    expect(seen?.get('Authorization')).toBe('Bearer tok123')
  })

  it('omits the Authorization header when the token is null', async () => {
    let seen: Headers | undefined
    const base = (async (_url: string, init?: RequestInit) => {
      seen = new Headers(init?.headers)
      return { ok: true, status: 200 } as Response
    }) as unknown as typeof fetch
    const f = makeAuthFetch(null, base)
    await f('/api/rescan', { method: 'POST' })
    expect(seen?.has('Authorization')).toBe(false)
  })

  it('preserves caller-supplied headers', async () => {
    let seen: Headers | undefined
    const base = (async (_url: string, init?: RequestInit) => {
      seen = new Headers(init?.headers)
      return { ok: true, status: 200 } as Response
    }) as unknown as typeof fetch
    await makeAuthFetch('tok', base)('/api/x', {
      headers: { 'Content-Type': 'application/json' },
    })
    expect(seen?.get('Content-Type')).toBe('application/json')
    expect(seen?.get('Authorization')).toBe('Bearer tok')
  })
})

describe('login', () => {
  it('posts the password and returns the token', async () => {
    let body: string | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      body = init?.body as string
      return { ok: true, status: 200, json: async () => ({ token: 'abc' }) } as Response
    }) as unknown as typeof fetch
    expect(await login('pw', fetchFn)).toBe('abc')
    expect(JSON.parse(body!)).toEqual({ password: 'pw' })
  })

  it('throws on a non-OK response (wrong password)', async () => {
    const fetchFn = (async () => ({ ok: false, status: 401 }) as Response) as unknown as typeof fetch
    await expect(login('pw', fetchFn)).rejects.toThrow(/login failed: 401/)
  })
})

describe('logout', () => {
  it('posts with the bearer token and swallows errors', async () => {
    let seen: Headers | undefined
    const fetchFn = (async (_url: string, init?: RequestInit) => {
      seen = new Headers(init?.headers)
      return { ok: true, status: 200 } as Response
    }) as unknown as typeof fetch
    await logout('tok', fetchFn)
    expect(seen?.get('Authorization')).toBe('Bearer tok')
  })
})

describe('fetchAuthStatus', () => {
  it('parses the auth_required flag', async () => {
    const fetchFn = (async () => ({
      ok: true, status: 200, json: async () => ({ auth_required: true }),
    })) as unknown as typeof fetch
    expect(await fetchAuthStatus(fetchFn)).toEqual({ auth_required: true })
  })

  it('fails closed (auth required) on a non-OK probe', async () => {
    const fetchFn = (async () => ({ ok: false, status: 500 }) as Response) as unknown as typeof fetch
    expect(await fetchAuthStatus(fetchFn)).toEqual({ auth_required: true })
  })

  it('fails closed (auth required) when the probe throws', async () => {
    const fetchFn = (async () => {
      throw new Error('network down')
    }) as unknown as typeof fetch
    expect(await fetchAuthStatus(fetchFn)).toEqual({ auth_required: true })
  })
})
