import { useState } from 'react'
import { runRetag, type RetagState } from './retagJob'
import { useAuthContext } from './useAuth'

// React wrapper around the pure runRetag driver. "Placement mode" holds the file
// id awaiting a map click; pickLocation runs the re-tag for the clicked coordinate
// (confirm-gated) and fires onDone so the caller can reload the library feed.
export function useRetagJob(onDone?: () => void) {
  const { authFetch } = useAuthContext()
  const [targetId, setTargetId] = useState<number | null>(null)
  const [retag, setRetag] = useState<RetagState | null>(null)
  const [retagging, setRetagging] = useState(false)

  const beginRetag = (fileId: number): void => setTargetId(fileId)
  const cancelRetag = (): void => setTargetId(null)

  async function pickLocation(lng: number, lat: number): Promise<void> {
    if (targetId === null || retagging) return
    if (!window.confirm('Move this capture to the clicked location?')) return
    const fileId = targetId
    setTargetId(null)
    setRetagging(true)
    try {
      const final = await runRetag(authFetch, fileId, lat, lng, { onProgress: setRetag })
      setRetag(final)
      onDone?.()
    } catch (e) {
      setRetag({
        state: 'error', status: 'failed', moved: 0, place_string: null,
        processed: 0, current: null, error: String(e),
      })
    } finally {
      setRetagging(false)
    }
  }

  return { retag, retagging, placing: targetId !== null, beginRetag, cancelRetag, pickLocation }
}
