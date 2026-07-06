// Shared driver for every background job: POST a start request, read {job_id},
// poll the status URL to a terminal state. Each *Job.ts module wraps this with
// its own start URL/body and state type — the POST→poll→terminal loop lives
// only here (it was previously copy-pasted six times).

export interface JobSpec {
  // Label used in error messages ('organize', 'undo', ...).
  kind: string
  startUrl: string
  // Defaults to a bare POST; pass headers/body for JSON-bodied starts.
  startInit?: RequestInit
  statusUrl: (jobId: string) => string
  // States that end polling. Defaults to done/error/cancelled.
  terminal?: ReadonlySet<string>
}

export interface PollOpts<S> {
  onProgress?: (s: S) => void
  intervalMs?: number
  // Watchdog: reject when the job hasn't reached a terminal state after this
  // long, so a wedged backend job can't keep the client polling forever.
  // Omit (undefined) for jobs with no meaningful upper bound (a bulk organize
  // scales with inbox size).
  timeoutMs?: number
}

const DEFAULT_TERMINAL: ReadonlySet<string> = new Set(['done', 'error', 'cancelled'])

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

export async function pollJob<S extends { state: string }>(
  fetchFn: typeof fetch,
  spec: JobSpec,
  opts: PollOpts<S> = {},
): Promise<S> {
  const { onProgress, intervalMs = 500, timeoutMs } = opts
  const started = await fetchFn(spec.startUrl, spec.startInit ?? { method: 'POST' })
  if (!started.ok) throw new Error(`${spec.kind} start failed: ${started.status}`)
  const { job_id } = (await started.json()) as { job_id: string }

  const terminal = spec.terminal ?? DEFAULT_TERMINAL
  const deadline = timeoutMs !== undefined ? Date.now() + timeoutMs : null
  for (;;) {
    const resp = await fetchFn(spec.statusUrl(job_id))
    if (!resp.ok) throw new Error(`${spec.kind} status failed: ${resp.status}`)
    const state = (await resp.json()) as S
    onProgress?.(state)
    if (terminal.has(state.state)) return state
    if (deadline !== null && Date.now() >= deadline) {
      throw new Error(
        `${spec.kind} timed out: no terminal state after ${Math.round(timeoutMs! / 1000)}s`,
      )
    }
    await sleep(intervalMs)
  }
}
