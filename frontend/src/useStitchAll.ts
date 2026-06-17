import { useCallback, useRef, useState } from 'react'
import { runStitchAll, type StitchAllProgress, type StitchAllSummary } from './stitchAllJob'
import { useAuthContext } from './useAuth'

// Drives the optional, interruptible "Stitch all panoramas" toolbar action. The
// stitch pool is independent of the destructive worker, so this can run while the
// user browses; `cancel()` flips a ref polled between ids so the batch stops after
// the in-flight stitch. `onComplete` reloads the library so finished panoramas drop
// out of the target set. There is NO automatic invocation — only an explicit click.
export function useStitchAll(onComplete: () => void) {
  const { authFetch } = useAuthContext()
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<StitchAllProgress | null>(null)
  // The last completed run's summary (incl. failedIds), so the UI can report which
  // panoramas failed after the batch finishes. Cleared at the start of a new run.
  const [result, setResult] = useState<StitchAllSummary | null>(null)
  const cancelled = useRef(false)

  const start = useCallback(
    (ids: number[]) => {
      if (ids.length === 0 || running) return
      cancelled.current = false
      setRunning(true)
      setResult(null)
      setProgress({ done: 0, total: ids.length, current: ids[0] })
      runStitchAll(authFetch, ids, {
        shouldContinue: () => !cancelled.current,
        onProgress: setProgress,
      })
        .then(setResult)
        .catch(() => undefined) // runStitchAll never rejects, but stay defensive
        .finally(() => {
          setRunning(false)
          setProgress(null)
          onComplete()
        })
    },
    [onComplete, running, authFetch],
  )

  const cancel = useCallback(() => {
    cancelled.current = true
  }, [])

  return { running, progress, result, start, cancel }
}
