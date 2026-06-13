import { useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { listThumb } from '../api'
import { columnsForWidth, rowCount, rowSlice } from '../gridWindow'
import type { LibraryFeature } from '../types'
import LoadingImage from './LoadingImage'

interface Props {
  files: LibraryFeature[]
  onOpen: (index: number) => void
  onRetag: (index: number) => void
}

export default function FileListPanel({ files, onOpen, onRetag }: Props) {
  const empty = files.length === 0
  const place = files[0]?.properties.place_string ?? ''
  const date = files[0]?.properties.local_date ?? ''
  // The viewport-driven list can span many places/dates, so only label it with the
  // first file's place when every file shares that place (a co-located point or a
  // tight cluster), and only append the date when they also share that date;
  // otherwise show a neutral count header.
  const onePlace = !empty && files.every((f) => f.properties.place_string === place)
  const oneDate = onePlace && files.every((f) => f.properties.local_date === date)

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

  // Virtualize the thumbnail grid: a cluster can hold hundreds of files, and
  // mounting every thumb would fire one /api/thumb request per file at once. We
  // window by ROW (each row is `columns` files) so only viewport-visible rows
  // mount their LoadingImage. Columns track the (resizable) panel width; the
  // ROW grid uses 1fr columns so cells always fit the width regardless of the
  // computed count (no horizontal overflow). The row-height estimate is the
  // cell width (square thumb, aspect-ratio:1) plus the filename + retag button;
  // `measureElement` corrects it from the real DOM height after first paint.
  const scrollRef = useRef<HTMLDivElement>(null)
  // GAP_PX must match the .grid-row CSS gap so the column count and the cell-width
  // estimate agree; PAD_PX is the .grid padding on both sides.
  const GAP_PX = 6
  const PAD_PX = 8
  const columns = columnsForWidth(width, 120, GAP_PX)
  const rows = rowCount(files.length, columns)
  const cellWidth = (width - 2 * PAD_PX - (columns - 1) * GAP_PX) / columns
  const estRow = Math.max(80, cellWidth + 46)

  const virtualizer = useVirtualizer({
    count: rows,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estRow,
    overscan: 2,
  })

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
          {empty ? (
            <strong>In view</strong>
          ) : onePlace ? (
            <>
              <strong>{place}</strong>
              <br />
              <small>{oneDate ? `${date} · ` : ''}{files.length} file(s)</small>
            </>
          ) : (
            <>
              <strong>In view</strong>
              <br />
              <small>{files.length} file(s)</small>
            </>
          )}
        </div>
      </div>
      {empty && <div className="panel-empty">No captures in view</div>}
      <div className="grid" ref={scrollRef}>
        <div className="grid-sizer" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((vrow) => {
            const { start, end } = rowSlice(vrow.index, columns, files.length)
            return (
              <div
                key={vrow.key}
                data-index={vrow.index}
                ref={virtualizer.measureElement}
                className="grid-row"
                style={{
                  transform: `translateY(${vrow.start}px)`,
                  gridTemplateColumns: `repeat(${columns}, 1fr)`,
                }}
              >
                {files.slice(start, end).map((f, j) => {
                  const i = start + j
                  return (
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
                  )
                })}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
