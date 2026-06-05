import { useRef, useState } from 'react'
import { listThumb } from '../api'
import type { LibraryFeature } from '../types'
import LoadingImage from './LoadingImage'

interface Props {
  files: LibraryFeature[]
  onOpen: (index: number) => void
  onRetag: (index: number) => void
  onClose: () => void
}

export default function FileListPanel({ files, onOpen, onRetag, onClose }: Props) {
  const place = files[0]?.properties.place_string ?? ''
  const date = files[0]?.properties.local_date ?? ''

  // Drag-to-resize: the panel is too narrow for long filenames at the default
  // width, so a left-edge handle lets the user widen it (clamped to a usable range).
  const [width, setWidth] = useState(380)
  const dragging = useRef(false)

  const onResizeDown = (e: React.PointerEvent) => {
    dragging.current = true
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onResizeMove = (e: React.PointerEvent) => {
    if (!dragging.current) return
    setWidth(Math.max(280, Math.min(900, window.innerWidth - e.clientX)))
  }
  const onResizeUp = (e: React.PointerEvent) => {
    dragging.current = false
    // Guarded: also runs on pointercancel, where capture may already be released.
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
  }

  return (
    <div className="panel" style={{ width }}>
      <div
        className="panel-resize"
        onPointerDown={onResizeDown}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeUp}
        onPointerCancel={onResizeUp}
        title="Drag to resize"
      />
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
              <span className="thumb-img">
                <LoadingImage
                  key={f.properties.path}
                  src={listThumb(f.properties.media_type, f.properties.path)}
                  alt={f.properties.filename}
                />
                {f.properties.capture_kind === 'hyperlapse' && (f.properties.frame_count ?? 0) > 0 && (
                  <span className="badge badge--hyperlapse" title="Hyperlapse render">
                    ⊞ ×{f.properties.frame_count}
                  </span>
                )}
                {f.properties.capture_kind === 'panorama' && (f.properties.frame_count ?? 0) > 0 && (
                  <span className="badge badge--panorama" title="Panorama">
                    ▦ ×{f.properties.frame_count}
                  </span>
                )}
                {f.properties.star_rating !== null && (
                  <span
                    className="badge badge--stars"
                    title={`${f.properties.star_rating}★ rating`}
                  >
                    {'★'.repeat(f.properties.star_rating)}
                    {'☆'.repeat(5 - f.properties.star_rating)}
                  </span>
                )}
              </span>
              <span className="thumb-name">
                {f.properties.media_type === 'video' ? '▶ ' : ''}{f.properties.filename}
              </span>
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
