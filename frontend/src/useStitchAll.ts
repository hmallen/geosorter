import { useCallback, useRef, useState } from 'react'
import { runStitchAll, type StitchAllProgress } from './stitchAllJob'

// Drives the optional, interruptible "Stitch all panoramas" toolbar action. The
// stitch pool is independent of the destructive worker, so this can run while the
// user browses; `cancel()` flips a ref polled between ids so the batch stops after
// the in-flight stitch. `onComplete` reloads the library so finished panoramas drop
// out of the target set. There is NO automatic invocation — only an explicit click.
export function useStitchAll(onComplete: () => void) {
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<StitchAllProgress | null>(null)
  const cancelled = useRef(false)

  const start = useCallback(
    (ids: number[]) => {
      if (ids.length === 0 || running) return
      cancelled.current = false
      setRunning(true)
      setProgress({ done: 0, total: ids.length, current: ids[0] })
      runStitchAll(fetch, ids, {
        shouldContinue: () => !cancelled.current,
        onProgress: setProgress,
      })
        .catch(() => undefined) // runStitchAll never rejects, but stay defensive
        .finally(() => {
          setRunning(false)
          setProgress(null)
          onComplete()
        })
    },
    [onComplete, running],
  )

  const cancel = useCallback(() => {
    cancelled.current = true
  }, [])

  return { running, progress, start, cancel }
}
