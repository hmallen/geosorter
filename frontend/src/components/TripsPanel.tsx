import { useMemo, useState } from 'react'
import {
  buildTrips,
  filterTrips,
  DEFAULT_GAP_DAYS,
  GAP_CHOICES,
  type Trip,
} from '../trips'
import type { LibraryFeature } from '../types'

interface Props {
  // The FULL library features (not the filtered view) — the trips list must not
  // collapse to one entry while a trip's own date filter is active.
  features: LibraryFeature[]
  onClose: () => void
  // Apply this trip: App sets the date-range filter and flies to the bbox.
  onPick: (trip: Trip) => void
}

// Auto-derived trips over the library: runs of capture days at most `gapDays`
// apart (see trips.buildTrips), newest first. Typing narrows the list; clicking
// a trip filters the map + file list to its date range and fits the camera to
// its captures. Reuses the LocationPanel's CSS classes — same panel anatomy.
export default function TripsPanel({ features, onClose, onPick }: Props) {
  const [query, setQuery] = useState('')
  const [gapDays, setGapDays] = useState<number>(DEFAULT_GAP_DAYS)
  const trips = useMemo(() => buildTrips(features, gapDays), [features, gapDays])
  const shown = useMemo(() => filterTrips(trips, query), [trips, query])

  return (
    <div className="location-panel">
      <div className="panel-head">
        <strong>Trips</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {trips.length === 0 ? (
        <p className="inbox-note">No dated captures in the library yet.</p>
      ) : (
        <>
          <label className="trips-gap">
            New trip after
            <select
              value={gapDays}
              onChange={(e) => setGapDays(Number(e.target.value))}
              aria-label="Idle days that start a new trip"
            >
              {GAP_CHOICES.map((g) => (
                <option key={g} value={g}>
                  {g} idle day{g === 1 ? '' : 's'}
                </option>
              ))}
            </select>
          </label>
          <input
            className="location-search"
            type="text"
            placeholder="Filter trips (e.g. Moab, Mar 2024)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {shown.length === 0 ? (
            <p className="inbox-note">No trips match “{query}”.</p>
          ) : (
            <ul className="location-list">
              {shown.map((t) => (
                <li key={t.key}>
                  <button
                    className="location-row"
                    onClick={() => onPick(t)}
                    title="Show only this trip and fit the map to it"
                  >
                    <span className="trip-main">
                      <span className="location-name">{t.placeLabel}</span>
                      <span className="trip-dates">{t.dateLabel}</span>
                    </span>
                    <span className="location-count">{t.count}</span>
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
