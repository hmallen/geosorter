import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import {
  clearToken,
  fetchAuthStatus,
  isAdmin as computeIsAdmin,
  loadToken,
  login as apiLogin,
  logout as apiLogout,
  makeAuthFetch,
  saveToken,
} from './auth'

// The auth state shared across the app. `isAdmin` decides whether the admin-only
// controls render and whether the mutating API calls carry a token.
interface AuthValue {
  // Whether the server has an admin password configured. Until the /api/auth probe
  // resolves it is false, so the app starts in its open default and only locks down
  // once we learn auth is required.
  authRequired: boolean
  // True when management actions are unlocked: either no password is configured
  // (open app) or the user has logged in.
  isAdmin: boolean
  // A fetch that injects the bearer token (a transparent pass-through when logged
  // out / unconfigured). Thread this into every mutating API call.
  authFetch: typeof fetch
  signIn: (password: string) => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authRequired, setAuthRequired] = useState(false)
  const [token, setToken] = useState<string | null>(() => loadToken())
  // Whether the /api/auth probe has resolved. Until it has, isAdmin is forced false
  // so the admin controls never FLASH on for an unauthenticated viewer before we know
  // whether the server is gated (the probe is fast + same-origin).
  const [ready, setReady] = useState(false)

  // Probe whether login is required once on mount. fetchAuthStatus fails CLOSED, so a
  // transient error leaves the app view-only rather than exposing the admin controls.
  useEffect(() => {
    let alive = true
    fetchAuthStatus().then((s) => {
      if (alive) {
        setAuthRequired(s.auth_required)
        setReady(true)
      }
    })
    return () => {
      alive = false
    }
  }, [])

  // The bearer-injecting fetch, wrapped to self-heal a stale token: a 401 means the
  // token is no longer valid (only the guarded mutating routes 401 — reads never do),
  // e.g. after a server restart cleared the in-memory store. Drop it so the UI returns
  // to logged-out instead of showing admin controls whose every action 401s.
  const authFetch = useMemo<typeof fetch>(() => {
    const base = makeAuthFetch(token)
    return (async (input, init) => {
      const resp = await base(input, init)
      if (resp.status === 401 && token) {
        clearToken()
        setToken(null)
      }
      return resp
    }) as typeof fetch
  }, [token])

  const signIn = useCallback(async (password: string) => {
    const tok = await apiLogin(password)
    saveToken(tok)
    setToken(tok)
  }, [])

  const signOut = useCallback(() => {
    if (token) apiLogout(token)
    clearToken()
    setToken(null)
  }, [token])

  // Admin when auth isn't required at all, or when we hold a token — but only once the
  // probe has resolved (avoids a pre-probe flash of admin controls).
  const isAdmin = ready && computeIsAdmin(authRequired, token)

  const value = useMemo<AuthValue>(
    () => ({ authRequired, isAdmin, authFetch, signIn, signOut }),
    [authRequired, isAdmin, authFetch, signIn, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// The provider and its consumer hook are colocated by design (one auth module).
// react-refresh's "only export components" rule is about HMR mechanics, not
// correctness, so it is scoped-off for this intentional pairing.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuthContext(): AuthValue {
  const ctx = useContext(AuthContext)
  if (ctx === null) throw new Error('useAuthContext must be used within an AuthProvider')
  return ctx
}
