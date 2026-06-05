import { useCallback, useState } from 'react'
import { fetchInboxList } from './api'
import type { InboxGroup } from './inboxTree'

// On-demand fetch of the inbox's capture groups for the import-selection panel.
// `load()` re-fetches (call it when the panel opens). Errors are surfaced via
// `error` so the panel can show a message rather than silently rendering empty.
export function useInboxList() {
  const [groups, setGroups] = useState<InboxGroup[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setGroups(await fetchInboxList())
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  return { groups, loading, error, load }
}
