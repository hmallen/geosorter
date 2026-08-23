// Pure drivers for the broken-capture repair jobs (m-repair-broken-captures):
// the ffprobe scan over the quarantine and one untrunc rebuild. Both POST a
// start request then poll a status URL to a terminal state via the shared
// pollJob loop; `fetchFn` is injectable for tests (and threads admin auth).

import { pollJob } from './pollJob'
import type { RepairItem } from './types'

export interface RepairScanState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  checked: number
  ok: number
  broken: number
  processed: number
  current: string | null
  untrunc_available: boolean
  items: RepairItem[]
  error: string | null
}

export async function runRepairScan(
  fetchFn: typeof fetch = fetch,
  opts: { onProgress?: (s: RepairScanState) => void; intervalMs?: number } = {},
): Promise<RepairScanState> {
  // A quarantine sweep is bounded (one fast ffprobe per file, and broken files
  // fail fastest) — minutes at worst over SMB.
  return pollJob<RepairScanState>(fetchFn, {
    kind: 'repair scan',
    startUrl: '/api/repair/scan',
    statusUrl: (id) => `/api/repair/scan/status/${id}`,
    terminal: new Set(['done', 'error']),
  }, { timeoutMs: 30 * 60_000, ...opts })
}

export interface RepairRunState {
  job_id?: string
  state: 'pending' | 'running' | 'done' | 'error'
  file_id: number | null
  reference_id: number | null
  phase: '' | 'backup' | 'repair' | 'verify'
  bytes_done: number
  bytes_total: number
  status: '' | 'ok' | 'failed'
  // Set on an 'ok' result that still looks wrong (e.g. untrunc recovered almost
  // no data from a mismatched reference) — shown prominently before accept.
  warning: string | null
  fixed_path: string | null // library-relative preview path when status === 'ok'
  codec: string | null
  width: number | null
  height: number | null
  duration_s: number | null
  size: number | null
  error: string | null
  output_tail: string[]
}

export async function runRepairFix(
  fetchFn: typeof fetch,
  fileId: number,
  referenceId: number,
  opts: { onProgress?: (s: RepairRunState) => void; intervalMs?: number } = {},
): Promise<RepairRunState> {
  // No watchdog timeout: backing up + rebuilding a near-4 GB clip over SMB is
  // legitimately long; the backend enforces its own untrunc hang backstop.
  return pollJob<RepairRunState>(fetchFn, {
    kind: 'repair',
    startUrl: '/api/repair/run',
    startInit: {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: fileId, reference_id: referenceId }),
    },
    statusUrl: (id) => `/api/repair/status/${id}`,
    terminal: new Set(['done', 'error']),
  }, { intervalMs: 1000, ...opts })
}

// Human-readable byte size for the panel rows ("3.77 GB", "512 MB", "0 B").
export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 'B'
  for (const next of units) {
    if (value < 1024) break
    value /= 1024
    unit = next
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(value >= 10 ? 1 : 2)} ${unit}`
}

// Status → the label + row treatment the panel renders.
export const STATUS_LABELS: Record<RepairItem['status'], string> = {
  'zero-byte': 'empty file',
  'no-moov': 'truncated recording',
  'decode-error': 'unreadable',
  missing: 'missing on disk',
}
