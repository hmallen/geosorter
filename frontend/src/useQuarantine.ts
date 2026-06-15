import { useCallback, useEffect, useState } from 'react'
import { fetchQuarantine } from './api'
import type { QuarantineItem } from './types'

// Fetch the no-GPS (quarantined) captures for the No-GPS panel + toolbar badge.
// `reload` is called after an assign/organize/undo run (items leave or enter
// quarantine). A failed fetch degrades to an empty list rather than throwing.
export function useQuarantine() {
  const [items, setItems] = useState<QuarantineItem[]>([])

  const reload = useCallback(() => {
    fetchQuarantine()
      .then(setItems)
      .catch(() => setItems([]))
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  return { items, count: items.length, reload }
}
