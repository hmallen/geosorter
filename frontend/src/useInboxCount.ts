import { useCallback, useEffect, useState } from 'react'
import { fetchInbox, type InboxCount } from './api'

// Polls /api/inbox on an interval so the toolbar badge reflects files dropped into
// the inbox while the app is open. `refresh()` re-fetches immediately (call it after
// an organize run so the badge drops without waiting for the next tick). Fetch errors
// are swallowed — the badge simply keeps its last value.
export function useInboxCount(intervalMs = 5000) {
  const [count, setCount] = useState<InboxCount>({ files: 0, captures: 0 })

  const refresh = useCallback(() => {
    fetchInbox()
      .then(setCount)
      .catch((e) => console.warn('inbox count refresh failed:', e))
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [refresh, intervalMs])

  return { count, refresh }
}
