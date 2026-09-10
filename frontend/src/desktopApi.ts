import { loadToken } from './auth'

export type DesktopPhase = 'preparing' | 'downloading' | 'extracting' | 'indexing' | 'validating' | 'done'
export interface DesktopCheck { name: string; ok: boolean; message: string }
export class DesktopActionError extends Error {
  technicalDetails?: string
  constructor(message: string, technicalDetails?: string) {
    super(message)
    this.technicalDetails = technicalDetails
  }
}

export interface DesktopJob {
  job_id: string
  kind: 'cities' | 'features' | 'untrunc'
  state: 'pending' | 'running' | 'done' | 'error' | 'interrupted'
  phase: DesktopPhase
  current: string
  done: number
  total: number
  message: string | null
  details?: string
}
export interface DesktopStatus {
  state: 'setup' | 'recovery' | 'ready' | 'closing'
  version: string
  configured: boolean
  config_path: string
  inbox_path: string
  library_root: string
  geonames_ready: boolean
  checks: DesktopCheck[]
  error: string | null
  error_details?: string | null
  job: DesktopJob | null
  active_jobs: { job_id: string; kind: string; state: string }[]
  auth_required: boolean
  extras: { hugin: boolean; hugin_message?: string | null; untrunc: boolean }
}

export async function desktopRequest<T>(route: string, body?: unknown): Promise<T> {
  const headers = new Headers()
  const token = loadToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api/desktop/${route}`, {
    method: body === undefined ? 'GET' : 'POST', headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const result = await response.json()
  if (!response.ok) throw new DesktopActionError(typeof result.detail === 'string' ? result.detail : 'GeoSorter could not complete that request. Please retry.', result.technical_details)
  return result as T
}

// Cache the handshake so React StrictMode never exchanges/removes a launch token twice.
let launch: Promise<{ status: DesktopStatus | null; settings: boolean }> | undefined
export function connectDesktop() {
  if (!launch) launch = (async () => {
    const params = new URLSearchParams(window.location.hash.slice(1))
    const token = params.get('desktop')
    const settings = params.get('settings') === '1'
    if (token) {
      params.delete('desktop')
      params.delete('settings')
      window.history.replaceState(null, '', window.location.pathname + window.location.search + (params.size ? `#${params}` : ''))
      await desktopRequest('session', { token })
    }
    const probe = await fetch('/api/desktop/status')
    if (probe.status === 404) return { status: null, settings: false }
    if (!probe.ok) throw new Error('Open GeoSorter from its shortcut to reconnect.')
    return { status: await probe.json() as DesktopStatus, settings }
  })().catch((error) => { launch = undefined; throw error })
  return launch
}

export function progressText(job: DesktopJob): string {
  if (job.state === 'error' || job.state === 'interrupted') return job.message || 'Setup needs attention.'
  const phase = { preparing: 'Preparing', downloading: job.kind === 'untrunc' ? 'Downloading video repair' : 'Downloading place data', extracting: 'Unpacking data',
    indexing: 'Building the place index', validating: 'Checking place data', done: 'Complete' }[job.phase] || job.phase
  return job.total > 0 ? `${phase} · ${Math.min(100, Math.floor(job.done / job.total * 100))}%` : phase
}
