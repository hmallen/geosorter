import { useCallback, useEffect, useState } from 'react'
import { fetchLibrary } from './api'
import type { LibraryFeature } from './types'

// Loads the full /api/library feed once (B6 serves it whole; clustering is client-side).
export function useLibrary() {
  const [features, setFeatures] = useState<LibraryFeature[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(() => {
    setLoading(true)
    fetchLibrary()
      .then((fc) => { setFeatures(fc.features); setError(null) })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => reload(), [reload])

  return { features, error, loading, reload }
}
