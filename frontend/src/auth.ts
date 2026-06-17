// Pure admin-auth helpers (m-implement-view-only-admin-auth): the bearer-token
// fetch wrapper, the login/logout/status API calls, and localStorage persistence.
// Kept side-effect-light and `fetchFn`-injectable so the logic is Vitest-testable
// without a real network or DOM (the React glue lives in useAuth.tsx).

const TOKEN_KEY = 'geosorter.admin_token'

export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function saveToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // localStorage unavailable (private mode / SSR) — the token simply lives only
    // in memory for this session.
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    // ignore — see saveToken
  }
}

// Whether management actions are unlocked: either the server requires no login
// (open app) or the user holds a token. The single source of truth for the gate,
// kept pure so it can be unit-tested without React.
export function isAdmin(authRequired: boolean, token: string | null): boolean {
  return !authRequired || token !== null
}

// Wrap a base fetch so every request carries `Authorization: Bearer <token>` when a
// token is present, while preserving any caller-supplied headers. With a null token
// it is a transparent pass-through (used in view-only / unconfigured mode).
export function makeAuthFetch(
  token: string | null,
  base: typeof fetch = fetch,
): typeof fetch {
  if (!token) return base
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers)
    headers.set('Authorization', `Bearer ${token}`)
    return base(input, { ...init, headers })
  }) as typeof fetch
}

// Exchange the admin password for a bearer token. Throws on a non-OK response
// (wrong password -> 401, auth not configured -> 400) so the caller can surface it.
export async function login(password: string, fetchFn: typeof fetch = fetch): Promise<string> {
  const resp = await fetchFn('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  if (!resp.ok) throw new Error(`login failed: ${resp.status}`)
  return ((await resp.json()) as { token: string }).token
}

// Revoke the current token server-side. Best-effort: a failure is swallowed (the
// client clears its token regardless).
export async function logout(token: string, fetchFn: typeof fetch = fetch): Promise<void> {
  try {
    await fetchFn('/api/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    // ignore — the local token is cleared by the caller anyway
  }
}

// Probe whether the server requires admin login. A failed/unreadable probe FAILS
// CLOSED (auth_required: true): if we cannot confirm the server is open, assume it is
// gated so a transient error never exposes the admin controls (this frontend always
// ships with the /api/auth route, so a 404 means something is wrong, not "old open
// backend"). The controls only unlock once a probe positively reports the app open or
// the user logs in.
export async function fetchAuthStatus(
  fetchFn: typeof fetch = fetch,
): Promise<{ auth_required: boolean }> {
  try {
    const resp = await fetchFn('/api/auth')
    if (!resp.ok) return { auth_required: true }
    return (await resp.json()) as { auth_required: boolean }
  } catch {
    return { auth_required: true }
  }
}
