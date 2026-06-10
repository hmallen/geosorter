import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { fetchInbox, type InboxCount } from './api'
import { createInboxPoll } from './inboxPoll'

// Polls /api/inbox on an interval so the toolbar badge reflects files dropped into the
// inbox while the app is open. An in-flight guard (createInboxPoll) prevents a slow SMB
// scan from stacking concurrent requests; `pausedRef` lets the caller suspend the
// interval while a destructive job runs (it otherwise competes with organize for the
// same SMB bandwidth and connection slots). `refresh()` re-fetches immediately (call it
// after a run so the badge drops without waiting for the next tick) and bypasses the
// pause gate. Fetch errors are swallowed — the badge keeps its last value.
export function useInboxCount(intervalMs = 5000, pausedRef?: RefObject<boolean>) {
  const [count, setCount] = useState<InboxCount>({ files: 0, captures: 0 })
  // Lazy-init so the driver (and its in-flight closure state) is created once, not
  // re-evaluated on every render.
  const pollRef = useRef<(() => void) | null>(null)
  if (pollRef.current === null) {
    pollRef.current = createInboxPoll(
      () => fetchInbox(),
      setCount,
      (e) => console.warn('inbox count refresh failed:', e),
    )
  }
  const refresh = useCallback(() => pollRef.current?.(), [])

  useEffect(() => {
    refresh()
    const id = setInterval(() => {
      if (!pausedRef?.current) refresh()
    }, intervalMs)
    return () => clearInterval(id)
  }, [refresh, intervalMs, pausedRef])

  return { count, refresh }
}
