import { useCallback, useEffect, useRef, useState } from 'react'
import { deleteVideoMarker, fetchVideoMarkers, saveVideoMarker, sortMarkers, type MarkerDraft, type VideoMarker } from './videoMarkers'

// Both lists live above the viewer. Independent generations keep a late read
// from replacing a newer mutation, library refresh, or selected video's data.
export function useVideoMarkers(fileId: number | null, libraryVersion: unknown, fetchFn: typeof fetch) {
  const [all, setAll] = useState<VideoMarker[]>([])
  const [current, setCurrent] = useState<{ fileId: number | null; markers: VideoMarker[] }>({ fileId: null, markers: [] })
  const [error, setError] = useState<string | null>(null)
  const [currentError, setCurrentError] = useState<{ fileId: number; message: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const allSeq = useRef(0)
  const currentSeq = useRef(0)

  const loadAll = useCallback(() => {
    const seq = ++allSeq.current
    return fetchVideoMarkers(fetchFn).then((markers) => {
      if (seq === allSeq.current) { setAll(markers); setError(null) }
    }).catch((e) => {
      if (seq === allSeq.current) setError(String(e))
    }).finally(() => {
      if (seq === allSeq.current) setLoading(false)
    })
  }, [fetchFn])
  const reload = useCallback(() => { setLoading(true); void loadAll() }, [loadAll])

  const loadCurrent = useCallback(() => {
    const seq = ++currentSeq.current
    if (fileId === null) return
    return fetchVideoMarkers(fetchFn, fileId).then((markers) => {
      if (seq === currentSeq.current) {
        setCurrent({ fileId, markers }); setCurrentError(null)
      }
    }).catch((e) => {
      if (seq === currentSeq.current) setCurrentError({ fileId, message: String(e) })
    })
  }, [fileId, fetchFn])
  const invalidateAll = useCallback(() => { allSeq.current++ }, [])
  const invalidateCurrent = useCallback(() => { currentSeq.current++ }, [])

  useEffect(() => {
    void loadAll()
    return invalidateAll
  }, [loadAll, libraryVersion, invalidateAll])
  useEffect(() => {
    void loadCurrent()
    return invalidateCurrent
  }, [loadCurrent, libraryVersion, invalidateCurrent])

  async function save(draft: MarkerDraft, id?: number) {
    if (fileId === null) throw new Error('This video is no longer available.')
    const seq = currentSeq.current
    const marker = await saveVideoMarker(fetchFn, fileId, draft, id)
    allSeq.current++
    if (seq === currentSeq.current) {
      currentSeq.current++
      setCurrent((c) => ({ fileId, markers: sortMarkers([
        ...(c.fileId === fileId ? c.markers : []).filter((m) => m.id !== marker.id), marker,
      ]) }))
      void loadCurrent()
    }
    void loadAll()
  }
  async function remove(id: number) {
    const seq = currentSeq.current
    await deleteVideoMarker(fetchFn, id)
    allSeq.current++
    setCurrent((c) => ({ ...c, markers: c.markers.filter((m) => m.id !== id) }))
    setAll((items) => items.filter((m) => m.id !== id))
    void loadAll()
    if (seq === currentSeq.current) void loadCurrent()
  }
  return { all, markers: current.fileId === fileId ? current.markers : [], error,
    currentError: currentError?.fileId === fileId ? currentError.message : null,
    loading, reload, retryCurrent: loadCurrent, save, remove }
}
