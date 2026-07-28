import { useCallback, useEffect, useState } from 'react'
import { fetchDuplicates } from './api'
import type { DuplicateItem } from './types'

// Fetch the duplicate-review backlog for the Duplicates panel + toolbar badge
// (mirrors useQuarantine). `reload` is called after an organize/undo run and
// after a dismiss. A failed fetch degrades to an empty list rather than throwing.
export function useDuplicates() {
  const [items, setItems] = useState<DuplicateItem[]>([])

  const reload = useCallback(() => {
    fetchDuplicates()
      .then(setItems)
      .catch(() => setItems([]))
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  return { items, count: items.length, reload }
}
