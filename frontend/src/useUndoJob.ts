import { useState } from 'react'
import { runUndo, type UndoState } from './undoJob'

// React wrapper around the pure runUndo driver. A confirm gate guards the
// destructive reverse-move; onDone fires after a run finishes so the caller can
// reload the library feed.
export function useUndoJob(onDone?: () => void) {
  const [undo, setUndo] = useState<UndoState | null>(null)
  const [undoing, setUndoing] = useState(false)

  async function startUndo(): Promise<void> {
    if (undoing) return
    if (!window.confirm('Undo the most recent batch? Its files move back to the inbox.')) return
    setUndoing(true)
    try {
      const final = await runUndo(fetch, { onProgress: setUndo })
      setUndo(final)
      onDone?.()
    } catch (e) {
      setUndo({
        state: 'error', batch_id: null, restored: 0, missing: 0, processed: 0,
        current: null, nothing_to_undo: false, conflicts: [], failures: [String(e)],
        error: String(e),
      })
    } finally {
      setUndoing(false)
    }
  }

  return { undo, undoing, startUndo }
}
