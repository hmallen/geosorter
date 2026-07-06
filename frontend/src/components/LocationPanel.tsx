import { useMemo, useState } from 'react'
import { filterPlaces, type Place } from '../locationFilter'
import type { BBox } from '../clusters'

interface Props {
  places: Place[]
  onClose: () => void
  // Move the map to fit this place's captures (its bounding box). The name rides
  // along so App can record the picked place as the URL-hash breadcrumb.
  onPick: (bbox: BBox, place: string) => void
}

// A text filter over every distinct place in the library. Typing narrows the list;
// clicking a place flies the map to fit that place's pins. The place list is derived
// client-side from the loaded features (see locationFilter.buildPlaces) — no API call.
export default function LocationPanel({ places, onClose, onPick }: Props) {
  const [query, setQuery] = useState('')
  const shown = useMemo(() => filterPlaces(places, query), [places, query])

  return (
    <div className="location-panel">
      <div className="panel-head">
        <strong>Locations</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {places.length === 0 ? (
        <p className="inbox-note">No located captures in the library yet.</p>
      ) : (
        <>
          <input
            className="location-search"
            type="text"
            placeholder="Filter places (e.g. Moab, Colorado)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {shown.length === 0 ? (
            <p className="inbox-note">No places match “{query}”.</p>
          ) : (
            <ul className="location-list">
              {shown.map((p) => (
                <li key={p.place_string}>
                  <button
                    className="location-row"
                    onClick={() => onPick(p.bbox, p.place_string)}
                    title={`Fly to ${p.place_string}`}
                  >
                    <span className="location-name">{p.place_string}</span>
                    <span className="location-count">{p.count}</span>
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
