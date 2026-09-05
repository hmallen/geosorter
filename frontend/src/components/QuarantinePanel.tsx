import { useState } from 'react'
import { listThumb, placeSearch } from '../api'
import LoadingImage from './LoadingImage'
import type { PlaceResult, QuarantineItem } from '../types'

interface Props {
  items: QuarantineItem[]
  // Captures already submitted to the assignment worker. They remain visible until
  // the quarantine feed reloads, but cannot be selected for a duplicate submission.
  queuedFileIds: ReadonlySet<number>
  onClose: () => void
  // Preview one capture's media in the lightbox (browse mode thumbnail click).
  onView: (item: QuarantineItem) => void
  // Enter map placement mode for the selected captures (the next map click assigns).
  onPickOnMap: (ids: number[]) => void
  // Assign the selected captures to an explicit coordinate (from place search).
  onAssignToPlace: (ids: number[], lat: number, lon: number) => void
}

// The No-GPS panel: lists quarantined captures with thumbnails + multi-select, and
// assigns one location to all selected either by clicking the map or by picking a
// place-name search match. Promotes them out of quarantine to organized.
export default function QuarantinePanel({
  items,
  queuedFileIds,
  onClose,
  onView,
  onPickOnMap,
  onAssignToPlace,
}: Props) {
  const [selected, setSelected] = useState<Set<number>>(new Set())
  // Multi-select mode: when on, a thumbnail click toggles selection (fast bulk
  // picking) instead of previewing the media. The top-left checkbox selects in
  // either mode; this just changes what the thumbnail itself does.
  const [multiSelect, setMultiSelect] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<PlaceResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  const availableIds = items.filter((item) => !queuedFileIds.has(item.id)).map((item) => item.id)
  const queuedItemCount = items.length - availableIds.length
  const allSelected = availableIds.length > 0 && availableIds.every((id) => selected.has(id))

  const toggle = (id: number, on: boolean) =>
    setSelected((prev) => {
      if (queuedFileIds.has(id)) return prev
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  const toggleAll = (on: boolean) =>
    setSelected(on ? new Set(availableIds) : new Set())

  async function doSearch(e: React.FormEvent) {
    e.preventDefault()
    setSearching(true)
    setSearchError(null)
    try {
      setResults(await placeSearch(query))
    } catch (err) {
      setSearchError(String(err))
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  const ids = [...selected].filter((id) => !queuedFileIds.has(id))
  function assignToPlace(m: PlaceResult) {
    if (!ids.length) return
    const label = m.place_string ?? m.name
    if (!window.confirm(`Assign "${label}" to ${ids.length} no-GPS capture(s)?`)) return
    onAssignToPlace(ids, m.lat, m.lon)
    onClose()
  }

  return (
    <div className="quarantine-panel">
      <div className="panel-head">
        <strong>No-GPS captures</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {items.length === 0 ? (
        <p className="inbox-note">No no-GPS captures to place.</p>
      ) : (
        <>
          <div className="quarantine-modebar">
            <button
              type="button"
              className={`quarantine-modebtn${multiSelect ? ' quarantine-modebtn--active' : ''}`}
              onClick={() => setMultiSelect((m) => !m)}
              aria-pressed={multiSelect}
            >
              {multiSelect ? '✓ Multi-select on' : 'Multi-select'}
            </button>
            <span className="quarantine-modehint">
              {multiSelect ? 'Click thumbnails to select' : 'Click a thumbnail to view'}
            </span>
          </div>

          <label className="inbox-selectall">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={(e) => toggleAll(e.target.checked)}
            />
            <span>
              {queuedItemCount > 0
                ? `Select all (${availableIds.length} available, ${queuedItemCount} queued)`
                : `Select all (${items.length} capture${items.length === 1 ? '' : 's'})`}
            </span>
          </label>

          <div className="quarantine-grid">
            {items.map((it) => (
              <div
                key={it.id}
                className={`quarantine-item${
                  selected.has(it.id) && !queuedFileIds.has(it.id)
                    ? ' quarantine-item--selected' : ''
                }${queuedFileIds.has(it.id) ? ' quarantine-item--queued' : ''}`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(it.id) && !queuedFileIds.has(it.id)}
                  disabled={queuedFileIds.has(it.id)}
                  onChange={(e) => toggle(it.id, e.target.checked)}
                  aria-label={`Select ${it.filename}`}
                />
                <button
                  type="button"
                  className="quarantine-thumb-btn"
                  onClick={() =>
                    multiSelect && !queuedFileIds.has(it.id)
                      ? toggle(it.id, !selected.has(it.id))
                      : onView(it)
                  }
                  title={
                    queuedFileIds.has(it.id)
                      ? 'Location assignment queued'
                      : multiSelect ? 'Click to select' : 'Click to view'
                  }
                >
                  <LoadingImage
                    className="quarantine-thumb"
                    src={listThumb(it.media_type, it.path)}
                    alt={it.filename}
                  />
                </button>
                <span className="quarantine-name">{it.filename}</span>
                {it.date && <span className="quarantine-date">{it.date}</span>}
                {queuedFileIds.has(it.id) && (
                  <span className="quarantine-queued">Location queued</span>
                )}
              </div>
            ))}
          </div>

          <div className="quarantine-actions">
            <button
              onClick={() => onPickOnMap(ids)}
              disabled={ids.length === 0}
              title="Click a point on the map to assign it to the selected captures"
            >
              Set location on map ({ids.length})
            </button>
          </div>

          <form className="quarantine-search" onSubmit={doSearch}>
            <input
              type="text"
              placeholder="Search a place name (e.g. Moab, Utah)"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button type="submit" disabled={searching || !query.trim()}>
              {searching ? 'Searching…' : 'Search'}
            </button>
          </form>
          {searchError && <p className="inbox-note inbox-error">{searchError}</p>}
          {results.length > 0 && (
            <ul className="place-results">
              {results.map((m) => (
                <li key={m.geonameid}>
                  <button
                    onClick={() => assignToPlace(m)}
                    disabled={ids.length === 0}
                    title={
                      ids.length === 0 ? 'Select captures first' : 'Assign to this place'
                    }
                  >
                    {m.place_string ?? m.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
