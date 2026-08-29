import { useEffect, useRef, useState } from 'react'
import {
  fetchRepairReferences,
  fetchUntrunc,
  listThumb,
  posterUrl,
  repairAccept,
  repairDelete,
  repairDiscard,
  setRepairNoGpsVisibility,
  videoUrl,
} from '../api'
import {
  STATUS_LABELS,
  fmtSize,
  runRepairFix,
  runRepairScan,
  type RepairRunState,
  type RepairScanState,
} from '../repairJob'
import { useAuthContext } from '../useAuth'
import type { RepairCandidate, RepairItem } from '../types'

interface Props {
  busy: boolean // a destructive job is running -> disable the mutating actions
  onClose: () => void
  // Fired after the library changed server-side (accept swapped a file / delete
  // pruned a row) so App reloads the feeds + quarantine badge.
  onChanged: () => void
}

// The active repair wizard for ONE broken capture: pick a reference, run
// untrunc, verify the output, accept or discard.
interface FixFlow {
  item: RepairItem
  candidates: RepairCandidate[] | null // null while loading
  selected: number | null
  run: RepairRunState | null
  running: boolean
  error: string | null
}

const PHASE_LABELS: Record<string, string> = {
  backup: 'Backing up the original…',
  repair: 'Rebuilding with untrunc…',
  verify: 'Verifying the result…',
}

// The Repair panel (m-repair-broken-captures): scans the quarantine for corrupt
// captures (0-byte, missing moov atom), suggests deleting empties, and walks a
// truncated clip through an untrunc rebuild — reference pick (best match
// recommended) → repair job with live progress → playable verification of the
// output → accept (swap into the library; backup retained) or discard.
export default function RepairPanel({ busy, onClose, onChanged }: Props) {
  const { authFetch } = useAuthContext()
  const [scan, setScan] = useState<RepairScanState | null>(null)
  const [scanError, setScanError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [items, setItems] = useState<RepairItem[]>([])
  const [untrunc, setUntrunc] = useState<{ available: boolean } | null>(null)
  const [flow, setFlow] = useState<FixFlow | null>(null)
  const [pendingId, setPendingId] = useState<number | null>(null) // item mutation in flight
  const [actionError, setActionError] = useState<string | null>(null)
  // The scan is owned by the panel and re-run on each open; the ref stops the
  // strict-mode double-mount from starting two identical sweeps.
  const startedRef = useRef(false)

  useEffect(() => {
    if (startedRef.current) return
    startedRef.current = true
    fetchUntrunc(authFetch).then(setUntrunc).catch(() => setUntrunc(null))
    void startScan()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function startScan(): Promise<void> {
    setScanning(true)
    setScanError(null)
    try {
      const final = await runRepairScan(authFetch, { onProgress: setScan })
      setScan(final)
      if (final.state === 'error') setScanError(final.error ?? 'scan failed')
      setItems(final.items)
    } catch (e) {
      setScanError(String(e))
    } finally {
      setScanning(false)
    }
  }

  async function beginFix(item: RepairItem): Promise<void> {
    setActionError(null)
    setFlow({ item, candidates: null, selected: null, run: null, running: false, error: null })
    try {
      const candidates = await fetchRepairReferences(authFetch, item.id)
      const recommended = candidates.find((c) => c.recommended)
      setFlow((f) =>
        f && f.item.id === item.id
          ? { ...f, candidates, selected: recommended?.id ?? candidates[0]?.id ?? null }
          : f,
      )
    } catch (e) {
      setFlow((f) => (f && f.item.id === item.id ? { ...f, candidates: [], error: String(e) } : f))
    }
  }

  async function startFix(): Promise<void> {
    if (!flow || flow.selected === null || flow.running) return
    const { item, selected } = flow
    setFlow((f) => (f ? { ...f, running: true, run: null, error: null } : f))
    try {
      const final = await runRepairFix(authFetch, item.id, selected, {
        onProgress: (s) => setFlow((f) => (f && f.item.id === item.id ? { ...f, run: s } : f)),
      })
      setFlow((f) => (f && f.item.id === item.id ? { ...f, run: final, running: false } : f))
    } catch (e) {
      setFlow((f) =>
        f && f.item.id === item.id ? { ...f, running: false, error: String(e) } : f,
      )
    }
  }

  async function acceptFix(): Promise<void> {
    if (!flow || busy) return
    if (
      !window.confirm(
        `Replace ${flow.item.filename} with the repaired version? ` +
          'The pre-repair original is kept in _repair/backups/.',
      )
    )
      return
    setPendingId(flow.item.id)
    setActionError(null)
    try {
      await repairAccept(authFetch, flow.item.id)
      setItems((list) => list.filter((i) => i.id !== flow.item.id))
      setFlow(null)
      onChanged()
    } catch (e) {
      setActionError(String(e))
    } finally {
      setPendingId(null)
    }
  }

  async function discardFix(): Promise<void> {
    if (!flow) return
    setPendingId(flow.item.id)
    setActionError(null)
    try {
      await repairDiscard(authFetch, flow.item.id)
      setFlow(null)
    } catch (e) {
      setActionError(String(e))
    } finally {
      setPendingId(null)
    }
  }

  async function deleteItem(item: RepairItem): Promise<void> {
    if (busy) return
    const what =
      item.status === 'missing'
        ? `Remove the index entry for ${item.filename}? The file is already gone from disk.`
        : `Permanently delete ${item.filename} (${fmtSize(item.size)}) from the library? ` +
          'The server re-checks that it is still broken before deleting.'
    if (!window.confirm(what)) return
    setPendingId(item.id)
    setActionError(null)
    try {
      await repairDelete(authFetch, item.id)
      setItems((list) => list.filter((i) => i.id !== item.id))
      if (flow?.item.id === item.id) setFlow(null)
      onChanged()
    } catch (e) {
      setActionError(String(e))
    } finally {
      setPendingId(null)
    }
  }

  async function setNoGpsVisibility(item: RepairItem, hidden: boolean): Promise<void> {
    if (busy) return
    setPendingId(item.id)
    setActionError(null)
    try {
      await setRepairNoGpsVisibility(authFetch, item.id, hidden)
      setItems((list) =>
        list.map((current) =>
          current.id === item.id
            ? { ...current, hidden_from_no_gps: hidden }
            : current,
        ),
      )
      onChanged()
    } catch (e) {
      setActionError(String(e))
    } finally {
      setPendingId(null)
    }
  }

  const repairable = (item: RepairItem): boolean =>
    item.media_type === 'video' &&
    (item.status === 'no-moov' || item.status === 'decode-error') &&
    untrunc?.available === true

  return (
    <div className="quarantine-panel repair-panel">
      <div className="panel-head">
        <strong>Repair broken files</strong>
        <button onClick={onClose} aria-label="Close repair panel">×</button>
      </div>

      {untrunc !== null && !untrunc.available && (
        <p className="repair-note repair-note--warn">
          untrunc was not found, so repairs are unavailable — install it and set
          <code> untrunc_path</code> in geosorter.toml. Scanning and deleting
          still work.
        </p>
      )}

      {scanning && (
        <p className="repair-note">
          <span className="spinner" aria-hidden="true" /> Scanning quarantined
          captures… {scan?.processed ?? 0} checked
          {scan?.current ? ` — ${scan.current}` : ''}
        </p>
      )}
      {scanError && (
        <p className="repair-note repair-note--error" role="alert">
          Scan failed — {scanError}
          <button className="repair-btn" onClick={startScan}>Retry</button>
        </p>
      )}
      {!scanning && !scanError && scan?.state === 'done' && (
        <p className="repair-note">
          {scan.checked} capture{scan.checked === 1 ? '' : 's'} checked —{' '}
          {items.length === 0
            ? 'no broken files found.'
            : `${items.length} broken.`}
          <button className="repair-btn" onClick={startScan} disabled={scanning}>
            Re-scan
          </button>
        </p>
      )}
      {actionError && (
        <p className="repair-note repair-note--error" role="alert">{actionError}</p>
      )}

      {flow === null ? (
        <ul className="repair-list">
          {items.map((item) => (
            <li key={item.id} className="repair-item">
              <div className="repair-item__info">
                <span className="quarantine-name">{item.filename}</span>
                <span className="quarantine-date">
                  {item.date ?? 'unknown date'} · {fmtSize(item.size)} ·{' '}
                  <span
                    className={`repair-badge repair-badge--${item.status}`}
                    title={item.error ?? undefined}
                  >
                    {STATUS_LABELS[item.status]}
                  </span>
                </span>
                {item.status === 'zero-byte' && (
                  <span className="repair-hint">
                    Nothing to recover — deleting is the only option.
                  </span>
                )}
                {item.status === 'missing' && (
                  <span className="repair-hint">
                    Stale entry — the file left the library (Rescan also clears these).
                  </span>
                )}
                {item.hidden_from_no_gps && (
                  <span className="repair-hint">Hidden from the No-GPS placement list.</span>
                )}
              </div>
              <div className="repair-item__actions">
                {repairable(item) && (
                  <button
                    className="repair-btn repair-btn--primary"
                    onClick={() => beginFix(item)}
                    disabled={pendingId !== null}
                    title="Rebuild this clip with untrunc using a healthy reference"
                  >
                    Repair…
                  </button>
                )}
                <button
                  className="repair-btn"
                  onClick={() => setNoGpsVisibility(item, !item.hidden_from_no_gps)}
                  disabled={busy || pendingId !== null}
                  title={
                    item.hidden_from_no_gps
                      ? 'Restore this capture to the No-GPS placement list'
                      : 'Keep the file but remove it from the No-GPS placement list'
                  }
                >
                  {pendingId === item.id
                    ? 'Working…'
                    : item.hidden_from_no_gps
                      ? 'Show in No GPS'
                      : 'Hide from No GPS'}
                </button>
                <button
                  className="repair-btn"
                  onClick={() => deleteItem(item)}
                  disabled={busy || pendingId !== null}
                  title={
                    item.status === 'missing'
                      ? 'Remove the stale index entry'
                      : 'Delete the broken file from disk (re-verified server-side)'
                  }
                >
                  {pendingId === item.id
                    ? 'Working…'
                    : item.status === 'missing'
                      ? 'Remove entry'
                      : 'Delete'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="repair-flow">
          <p className="repair-note">
            <button
              className="repair-btn"
              onClick={() => setFlow(null)}
              disabled={flow.running}
            >
              ← Back
            </button>{' '}
            Repairing <strong>{flow.item.filename}</strong> ({fmtSize(flow.item.size)})
          </p>

          {flow.run === null && !flow.running && (
            <>
              <p className="repair-note">
                Pick a healthy clip from the same drone and settings as the
                reference — untrunc copies its structure to rebuild the broken
                index.
              </p>
              {flow.candidates === null && (
                <p className="repair-note">
                  <span className="spinner" aria-hidden="true" /> Ranking reference clips…
                </p>
              )}
              {flow.error && (
                <p className="repair-note repair-note--error" role="alert">{flow.error}</p>
              )}
              {flow.candidates !== null && flow.candidates.length === 0 && !flow.error && (
                <p className="repair-note">
                  No healthy video in the library can serve as a reference.
                </p>
              )}
              {flow.candidates !== null && flow.candidates.length > 0 && (
                <ul className="repair-refs">
                  {flow.candidates.map((c) => (
                    <li key={c.id}>
                      <label
                        className={`repair-ref${
                          flow.selected === c.id ? ' repair-ref--selected' : ''
                        }${c.recommended ? ' repair-ref--recommended' : ''}`}
                      >
                        <input
                          type="radio"
                          name="repair-reference"
                          checked={flow.selected === c.id}
                          onChange={() => setFlow((f) => (f ? { ...f, selected: c.id } : f))}
                        />
                        <img
                          className="repair-ref__thumb"
                          src={listThumb('video', c.path)}
                          alt=""
                          loading="lazy"
                        />
                        <span className="repair-ref__meta">
                          <span className="quarantine-name">
                            {c.filename}
                            {c.recommended && (
                              <span className="repair-badge repair-badge--recommended">
                                Recommended
                              </span>
                            )}
                          </span>
                          <span className="quarantine-date">
                            {[
                              c.date,
                              c.codec,
                              c.width && c.height ? `${c.width}×${c.height}` : null,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                          </span>
                          {c.reasons.length > 0 && (
                            <span className="repair-hint">{c.reasons.join('; ')}</span>
                          )}
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
              <div className="repair-flow-actions">
                <button
                  className="repair-btn repair-btn--primary"
                  onClick={startFix}
                  disabled={flow.selected === null || flow.candidates === null}
                >
                  Start repair
                </button>
              </div>
            </>
          )}

          {(flow.running || flow.run?.state === 'running' || flow.run?.state === 'pending') && (
            <div className="repair-progress">
              <p className="repair-note">
                <span className="spinner" aria-hidden="true" />{' '}
                {PHASE_LABELS[flow.run?.phase ?? ''] ?? 'Starting…'}
              </p>
              {flow.run && flow.run.bytes_total > 0 && (
                <progress
                  value={Math.min(flow.run.bytes_done, flow.run.bytes_total)}
                  max={flow.run.bytes_total}
                />
              )}
            </div>
          )}

          {!flow.running && flow.run?.state === 'error' && (
            <p className="repair-note repair-note--error" role="alert">
              Repair failed — {flow.run.error}
            </p>
          )}
          {!flow.running && flow.run?.state === 'done' && flow.run.status === 'failed' && (
            <div>
              <p className="repair-note repair-note--error" role="alert">
                Repair failed — {flow.run.error}
              </p>
              {flow.run.output_tail.length > 0 && (
                <details className="repair-tail">
                  <summary>untrunc output</summary>
                  <pre>{flow.run.output_tail.join('\n')}</pre>
                </details>
              )}
            </div>
          )}

          {!flow.running &&
            flow.run?.state === 'done' &&
            flow.run.status === 'ok' &&
            flow.run.fixed_path && (
              <div className="repair-verify">
                {flow.run.warning && (
                  <p className="repair-note repair-note--warn" role="alert">
                    {flow.run.warning}
                  </p>
                )}
                <p className="repair-note">
                  Rebuild verified —{' '}
                  {[
                    flow.run.codec,
                    flow.run.width && flow.run.height
                      ? `${flow.run.width}×${flow.run.height}`
                      : null,
                    flow.run.duration_s != null
                      ? `${Math.round(flow.run.duration_s)}s`
                      : null,
                    flow.run.size != null ? fmtSize(flow.run.size) : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                  . Check the playback, then accept or discard.
                </p>
                <video
                  className="repair-video"
                  controls
                  preload="metadata"
                  src={videoUrl(flow.run.fixed_path)}
                  poster={posterUrl(flow.run.fixed_path)}
                />
                <div className="repair-flow-actions">
                  <button
                    className="repair-btn repair-btn--primary"
                    onClick={acceptFix}
                    disabled={busy || pendingId !== null}
                  >
                    {pendingId === flow.item.id ? 'Working…' : 'Accept — replace original'}
                  </button>
                  <button
                    className="repair-btn"
                    onClick={discardFix}
                    disabled={pendingId !== null}
                  >
                    Discard
                  </button>
                </div>
              </div>
            )}
        </div>
      )}
    </div>
  )
}
