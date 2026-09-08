import { useMemo, useState } from 'react'
import { formatMarkerTime, groupMarkers, type VideoMarker } from '../videoMarkers'
import type { LibraryFeature } from '../types'

interface Props {
  markers: VideoMarker[]
  features: LibraryFeature[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onClose: () => void
  onPick: (marker: VideoMarker) => void
}

export default function MarkersPanel({ markers, features, loading, error, onRetry, onClose, onPick }: Props) {
  const [query, setQuery] = useState('')
  const groups = useMemo(() => groupMarkers(markers, features, query), [markers, features, query])
  return <section className="location-panel markers-panel" aria-label="All video markers">
    <div className="panel-head"><strong>Markers</strong><button onClick={onClose} aria-label="Close markers">×</button></div>
    <input className="location-search" aria-label="Filter video markers" placeholder="Search filenames or notes" value={query} onChange={(e) => setQuery(e.target.value)} />
    {loading && <p role="status">Loading markers…</p>}
    {error && <p role="alert">{error} <button onClick={onRetry}>Retry</button></p>}
    {!loading && !error && groups.length === 0 && <p className="inbox-note">{query ? 'No matching markers.' : 'No markers yet. Open a video to add one.'}</p>}
    <div className="marker-groups">{groups.map(({ file, markers: items }) => <section key={file.properties.id}>
      <h3>{file.properties.filename}</h3><small>{file.properties.capture_ts_local?.slice(0, 10) ?? file.properties.local_date}</small>
      <ul className="marker-list">{items.map((marker) => <li key={marker.id}>
        <button className="marker-jump" onClick={() => onPick(marker)}>
          <time>{formatMarkerTime(marker.time_s)}</time><span>{marker.note || 'Untitled marker'}</span>
        </button>
      </li>)}</ul>
    </section>)}</div>
  </section>
}
