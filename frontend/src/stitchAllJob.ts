// Pure sequential driver for the optional "Stitch all panoramas" action
// (m-frontend-pano-ux). It stitches the given panorama file_ids ONE AT A TIME via
// the existing per-file runStitch (the server's stitch pool serializes anyway, but
// driving them sequentially keeps progress legible and the action interruptible).
// There is NO automatic background stitch — this only runs on an explicit click.

import { runStitch } from './stitchJob'

export interface StitchAllProgress {
  done: number // ids fully processed (ok / failed / threw)
  total: number
  current: number | null // file_id currently stitching, or null between ids
}

export interface StitchAllSummary {
  completed: number // status === 'ok'
  failed: number // status !== 'ok' or the stitch threw
  failedIds: number[] // the file_ids that failed — so the UI can point at them
  cancelled: boolean // stopped early because shouldContinue returned false
}

export interface StitchAllOpts {
  // Polled BETWEEN ids (never mid-stitch); returning false stops after the
  // in-flight stitch finishes — that is the interrupt.
  shouldContinue?: () => boolean
  onProgress?: (p: StitchAllProgress) => void
  intervalMs?: number
  // Injectable for tests; defaults to the real per-file runStitch.
  runStitchFn?: typeof runStitch
}

export async function runStitchAll(
  fetchFn: typeof fetch,
  ids: number[],
  opts: StitchAllOpts = {},
): Promise<StitchAllSummary> {
  const { shouldContinue, onProgress, intervalMs, runStitchFn = runStitch } = opts
  let completed = 0
  const failedIds: number[] = []
  for (let i = 0; i < ids.length; i++) {
    if (shouldContinue && !shouldContinue()) {
      return { completed, failed: failedIds.length, failedIds, cancelled: true }
    }
    const id = ids[i]
    onProgress?.({ done: i, total: ids.length, current: id })
    try {
      const st = await runStitchFn(fetchFn, id, { intervalMs })
      if (st.status === 'ok') completed += 1
      else if (st.status === 'unavailable') {
        // Hugin not installed: the panorama is not a failure (the backend leaves
        // stitch_status NULL, so no failed badge renders) — exclude it from failedIds
        // so the "N stitch(es) failed" note doesn't point at non-existent badges.
      } else failedIds.push(id)
    } catch {
      failedIds.push(id) // a thrown stitch (network/start error) is one failure, not a stop
    }
    onProgress?.({ done: i + 1, total: ids.length, current: null })
  }
  return { completed, failed: failedIds.length, failedIds, cancelled: false }
}
