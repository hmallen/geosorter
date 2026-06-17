import { useState } from 'react'
import { runRescan, type RescanState } from './rescanJob'
import { useAuthContext } from './useAuth'

// React wrapper around the pure runRescan driver. A confirm gate guards the
// (index-only) prune; onDone fires after a run finishes so the caller can reload
// the library feed + inbox badge.
export function useRescanJob(onDone?: () => void) {
  const { authFetch } = useAuthContext()
  const [rescan, setRescan] = useState<RescanState | null>(null)
  const [rescanning, setRescanning] = useState(false)

  async function startRescan(): Promise<void> {
    if (rescanning) return
    if (
      !window.confirm(
        'Rescan the library? Index entries for files no longer on disk are removed.',
      )
    )
      return
    setRescanning(true)
    try {
      const final = await runRescan(authFetch, { onProgress: setRescan })
      setRescan(final)
      onDone?.()
    } catch (e) {
      setRescan({
        state: 'error', checked: 0, pruned: 0, kept: 0, processed: 0,
        current: null, warnings: [], orphaned: [], error: String(e),
      })
    } finally {
      setRescanning(false)
    }
  }

  return { rescan, rescanning, startRescan }
}
