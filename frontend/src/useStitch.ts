import { useCallback, useRef, useState } from 'react'
import { runStitch, type StitchState } from './stitchJob'
import { useAuthContext } from './useAuth'

// App-level panorama-stitch tracking, keyed by file_id. The stitch is a ~7-min job;
// keeping its poll loop here (above the lightbox) means closing/reopening the
// lightbox no longer loses the in-progress state or freezes the step label. A
// per-file inflight guard dedups starts client-side (the server dedups too), and
// `onComplete` reloads the library on success so the hero + stitch_status persist.
export function useStitch(onComplete: () => void) {
  const { authFetch } = useAuthContext()
  const [byFile, setByFile] = useState<Record<number, StitchState>>({})
  const inflight = useRef<Set<number>>(new Set())

  const start = useCallback(
    (fileId: number, opts?: { force?: boolean; projection?: string }) => {
      if (inflight.current.has(fileId)) return // already pending/running for this file
      inflight.current.add(fileId)
      setByFile((m) => ({
        ...m,
        [fileId]: { state: 'pending', status: '', file_id: fileId, error: null },
      }))
      runStitch(authFetch, fileId, {
        onProgress: (st) => setByFile((m) => ({ ...m, [fileId]: st })),
        ...opts,
      })
        .then((st) => {
          setByFile((m) => ({ ...m, [fileId]: st }))
          if (st.status === 'ok') onComplete()
        })
        .catch(() =>
          setByFile((m) => ({
            ...m,
            [fileId]: { state: 'error', status: 'failed', file_id: fileId, error: 'stitch failed' },
          })),
        )
        .finally(() => inflight.current.delete(fileId))
    },
    [onComplete, authFetch],
  )

  return { byFile, start }
}
