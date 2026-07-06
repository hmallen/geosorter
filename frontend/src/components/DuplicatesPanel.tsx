import { useState } from 'react'
import type { DuplicateItem } from '../types'

interface Props {
  items: DuplicateItem[]
  onClose: () => void
  // Dismiss these rows: App threads authFetch into dismissDuplicates, reloads the
  // backlog and fires its changed path. Rejects on failure (surfaced inline here).
  onDismiss: (ids: number[]) => Promise<void>
}

// The duplicate-review panel: inbox captures organize skipped as duplicates of
// already-organized files (relocate_duplicates off). Dismiss moves the capture
// group to <inbox>/_duplicates/ and drops the row, draining the backlog without
// deleting anything. Styled like QuarantinePanel (top-left panel + .panel-head).
export default function DuplicatesPanel({ items, onClose, onDismiss }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function dismiss(ids: number[]) {
    if (busy || ids.length === 0) return
    setBusy(true)
    setError(null)
    try {
      await onDismiss(ids)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="duplicates-panel">
      <div className="panel-head">
        <strong>Duplicates</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {items.length > 0 && (
        <div className="dup-actions">
          <button
            onClick={() => dismiss(items.map((i) => i.id))}
            disabled={busy}
            title="Move every listed capture group to the inbox's _duplicates/ folder"
          >
            {busy ? 'Dismissing…' : `Dismiss all (${items.length})`}
          </button>
        </div>
      )}

      {error && <p className="inbox-note inbox-error">{error}</p>}

      {items.length === 0 ? (
        <p className="inbox-note">No duplicates awaiting review.</p>
      ) : (
        <ul className="dup-list">
          {items.map((it) => (
            <li key={it.id} className="dup-item">
              <div className="dup-meta">
                <span className="dup-name" title={it.source_path}>
                  <strong>{it.filename}</strong>
                  {it.missing && (
                    <span className="dup-missing" title="Source file no longer on disk — dismissing just clears the entry">
                      {' '}(missing)
                    </span>
                  )}
                </span>
                <span className="dup-path" title={it.matched_path ?? undefined}>
                  {it.matched_path
                    ? `matches ${it.matched_path}`
                    : 'matched file no longer in the library'}
                </span>
                <span className="dup-date">first seen {it.first_seen_at.slice(0, 10)}</span>
              </div>
              <button
                className="dup-dismiss"
                onClick={() => dismiss([it.id])}
                disabled={busy}
              >
                Dismiss
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
