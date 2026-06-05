import { useState } from 'react'
import { runOrganize, type JobState } from './organizeJob'

// React wrapper around the pure runOrganize driver. onDone fires after a run
// finishes so the caller can reload the library feed.
export function useOrganizeJob(onDone?: () => void) {
  const [job, setJob] = useState<JobState | null>(null)
  const [running, setRunning] = useState(false)
  // Expected capture count for this run (drives the import progress bar); null when
  // unknown. `total` is supplied by the caller (the inbox panel's selected count).
  const [total, setTotal] = useState<number | null>(null)

  async function start(primaries?: string[] | null, expected?: number | null): Promise<void> {
    if (running) return
    setTotal(expected ?? null)
    setRunning(true)
    try {
      const final = await runOrganize(fetch, { onProgress: setJob }, primaries ?? null)
      setJob(final)
    } catch (e) {
      setJob({
        state: 'error', organized: 0, quarantined: 0, duplicates_skipped: 0,
        companions: 0, processed: 0, current: null, current_phase: null,
        bytes_done: 0, bytes_total: 0, failures: [String(e)], error: String(e),
      })
    } finally {
      setRunning(false)
      // Always refresh the library + inbox badge — even on an errored run, where
      // group-atomic moves may have filed some captures before the failure.
      onDone?.()
    }
  }

  return { job, running, total, start }
}
