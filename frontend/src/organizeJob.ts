// Pure driver for the B6 background organize job: POST /api/organize then poll
// /api/organize/status/{id} to a terminal state. `fetchFn` is injectable for tests.

export interface JobState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  organized: number
  quarantined: number
  duplicates_skipped: number
  companions: number
  processed: number
  current: string | null
  current_phase: string | null
  bytes_done: number
  bytes_total: number
  failures: string[]
  error: string | null
}

// Human-readable byte size for the progress label.
export function fmtBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1) } MB`
  if (n >= 1024) return `${Math.round(n / 1024)} KB`
  return `${n} B`
}

// Live label while a run is in flight. Shows per-file byte progress (phase +
// done/total + percent) once a byte tick has arrived, else the file counter.
export function progressLabel(job: JobState): string {
  if (job.bytes_total > 0) {
    const pct = Math.floor((job.bytes_done / job.bytes_total) * 100)
    const phase = job.current_phase ?? 'processing'
    const name = job.current ? `${job.current} ` : ''
    return `${phase} ${name}— ${fmtBytes(job.bytes_done)}/${fmtBytes(job.bytes_total)} (${pct}%)`
  }
  return `processing ${job.processed}${job.current ? ` — ${job.current}` : ''}`
}

// Capture-level progress label for the import progress bar: how many capture
// groups have been processed of the total selected, and how many remain. `processed`
// is clamped to `total` because a skipped duplicate / already-moved group can tick
// the counter without being one of the expected captures.
export function loadProgressLabel(processed: number, total: number): string {
  const done = Math.min(processed, total)
  const remaining = Math.max(0, total - processed)
  return `loaded ${done} of ${total} capture${total === 1 ? '' : 's'} (${remaining} remaining)`
}

// Terminal label. On error, append the actual failure detail (job.error, or the
// joined failures) so the toolbar never shows a bare "errors N" with no reason.
export function resultLabel(job: JobState): string {
  let label = `${job.state}: organized ${job.organized}, quarantined ${job.quarantined}`
  if (job.failures.length) label += `, errors ${job.failures.length}`
  if (job.state === 'error') {
    const detail = job.error ?? (job.failures.length ? job.failures.join('; ') : '')
    if (detail) label += ` — ${detail}`
  }
  return label
}

const TERMINAL = new Set(['done', 'error', 'cancelled'])
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function runOrganize(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: JobState) => void; intervalMs?: number } = {},
  primaries?: string[] | null,
): Promise<JobState> {
  const { onProgress, intervalMs = 500 } = opts
  // A partial import sends the chosen capture-group ids; a full import (select-all,
  // the default) sends no body so the backend imports the whole inbox.
  const init: RequestInit =
    primaries != null
      ? {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ primaries }),
        }
      : { method: 'POST' }
  const started = await fetchFn('/api/organize', init)
  if (!started.ok) throw new Error(`organize start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  for (;;) {
    const resp = await fetchFn(`/api/organize/status/${job_id}`)
    if (!resp.ok) throw new Error(`organize status failed: ${resp.status}`)
    const state = (await resp.json()) as JobState
    onProgress?.(state)
    if (TERMINAL.has(state.state)) return state
    await sleep(intervalMs)
  }
}
