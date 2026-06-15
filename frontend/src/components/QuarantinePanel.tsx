import { useState } from 'react'
import { listThumb, placeSearch } from '../api'
import LoadingImage from './LoadingImage'
import type { PlaceResult, QuarantineItem } from '../types'

interface Props {
  items: QuarantineItem[]
  busy: boolean // a destructive job is running -> disable the assign actions
  onClose: () => void
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
  busy,
  onClose,
  onPickOnMap,
  onAssignToPlace,
}: Props) {
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<PlaceResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  const allSelected = items.length > 0 && selected.size === items.length
  const toggle = (id: number, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  const toggleAll = (on: boolean) =>
    setSelected(on ? new Set(items.map((i) => i.id)) : new Set())

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

  const ids = [...selected]
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
          <label className="inbox-selectall">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={(e) => toggleAll(e.target.checked)}
            />
            <span>
              Select all ({items.length} capture{items.length === 1 ? '' : 's'})
            </span>
          </label>

          <div className="quarantine-grid">
            {items.map((it) => (
              <label key={it.id} className="quarantine-item">
                <input
                  type="checkbox"
                  checked={selected.has(it.id)}
                  onChange={(e) => toggle(it.id, e.target.checked)}
                />
                <LoadingImage
                  className="quarantine-thumb"
                  src={listThumb(it.media_type, it.path)}
                  alt={it.filename}
                />
                <span className="quarantine-name">{it.filename}</span>
                {it.date && <span className="quarantine-date">{it.date}</span>}
              </label>
            ))}
          </div>

          <div className="quarantine-actions">
            <button
              onClick={() => onPickOnMap(ids)}
              disabled={busy || ids.length === 0}
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
                    disabled={busy || ids.length === 0}
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
