import { thumbUrl } from '../api'
import type { LibraryFeature } from '../types'

interface Props {
  files: LibraryFeature[]
  onOpen: (index: number) => void
  onRetag: (index: number) => void
  onClose: () => void
}

export default function FileListPanel({ files, onOpen, onRetag, onClose }: Props) {
  const place = files[0]?.properties.place_string ?? ''
  const date = files[0]?.properties.local_date ?? ''

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <strong>{place}</strong>
          <br />
          <small>{date} · {files.length} file(s)</small>
        </div>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>
      <div className="grid">
        {files.map((f, i) => (
          <div key={f.properties.id} className="thumb">
            <button className="thumb-open" onClick={() => onOpen(i)}>
              <img src={thumbUrl(f.properties.path)} alt={f.properties.filename} loading="lazy" />
              <span>{f.properties.media_type === 'video' ? '▶ ' : ''}{f.properties.filename}</span>
            </button>
            <button
              className="retag"
              onClick={() => onRetag(i)}
              title="Re-tag this file's location by clicking the map"
            >
              ⌖ Re-tag location
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
