import { useState } from 'react'
import { useAuthContext } from '../useAuth'

// The admin login / logout control in the toolbar (m-implement-view-only-admin-auth).
// Renders nothing when the server requires no login (open app). When login is
// required it shows a "Log in" button that reveals an inline password form, or a
// "Log out" button once authenticated.
export default function LoginControl() {
  const { authRequired, isAdmin, signIn, signOut } = useAuthContext()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!authRequired) return null // open app — no login needed

  if (isAdmin) {
    return (
      <button className="login-control" onClick={signOut} title="Log out of admin">
        Log out
      </button>
    )
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await signIn(password)
      setOpen(false)
      setPassword('')
    } catch {
      setError('Incorrect password')
    } finally {
      setBusy(false)
    }
  }

  return open ? (
    <form className="login-form" onSubmit={submit}>
      <input
        type="password"
        value={password}
        autoFocus
        placeholder="Admin password"
        onChange={(e) => setPassword(e.target.value)}
      />
      <button type="submit" disabled={busy}>
        {busy ? '…' : 'Sign in'}
      </button>
      <button
        type="button"
        onClick={() => {
          setOpen(false)
          setError(null)
          setPassword('')
        }}
      >
        Cancel
      </button>
      {error && <span className="login-error">{error}</span>}
    </form>
  ) : (
    <button className="login-control" onClick={() => setOpen(true)} title="Log in as admin">
      Log in
    </button>
  )
}
