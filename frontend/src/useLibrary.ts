import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchLibrary } from './api'
import type { LibraryFeature } from './types'

// Loads the /api/library feed with ETag/conditional-GET revalidation: the stored
// ETag is sent as If-None-Match on every reload (after organize/undo/retag/rescan),
// so an unchanged library returns 304 and the prior features stay visible — no
// re-parse of the 8-12 MB payload, no blank map while revalidating.
export function useLibrary() {
  const [features, setFeatures] = useState<LibraryFeature[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const etagRef = useRef<string | null>(null)
  // Monotonic reload counter: only the LATEST in-flight reload may commit its
  // response, so overlapping reloads (organize onDone racing an earlier reload)
  // can't resolve out of order and overwrite newer features with stale ones.
  const seqRef = useRef(0)

  const reload = useCallback(() => {
    const seq = ++seqRef.current
    setLoading(true)
    fetchLibrary(fetch, etagRef.current)
      .then((res) => {
        if (seq !== seqRef.current) return // superseded by a newer reload
        // 304: keep the existing features (stale-while-revalidate); only replace
        // them when the server sent a fresh FeatureCollection.
        if (!res.notModified && res.fc) setFeatures(res.fc.features)
        etagRef.current = res.etag
        setError(null)
      })
      .catch((e) => {
        if (seq === seqRef.current) setError(String(e))
      })
      .finally(() => {
        if (seq === seqRef.current) setLoading(false)
      })
  }, [])

  useEffect(() => reload(), [reload])

  return { features, error, loading, reload }
}
