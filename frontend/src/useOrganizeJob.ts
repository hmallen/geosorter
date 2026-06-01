import { useState } from 'react'
import { runOrganize, type JobState } from './organizeJob'

// React wrapper around the pure runOrganize driver. onDone fires after a run
// finishes so the caller can reload the library feed.
export function useOrganizeJob(onDone?: () => void) {
  const [job, setJob] = useState<JobState | null>(null)
  const [running, setRunning] = useState(false)

  async function start(): Promise<void> {
    if (running) return
    setRunning(true)
    try {
      const final = await runOrganize(fetch, { onProgress: setJob })
      setJob(final)
      onDone?.()
    } catch (e) {
      setJob({
        state: 'error', organized: 0, quarantined: 0, duplicates_skipped: 0,
        companions: 0, processed: 0, current: null, failures: [String(e)], error: String(e),
      })
    } finally {
      setRunning(false)
    }
  }

  return { job, running, start }
}
