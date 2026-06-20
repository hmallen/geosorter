import { useEffect, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { listThumb } from '../api'
import { columnsForWidth, rowCount, rowSlice } from '../gridWindow'
import { useIsMobile } from '../useMediaQuery'
import { clampFraction, nearestSnap, cycleSnap, SHEET_SNAPS } from '../sheet'
import type { LibraryFeature } from '../types'
import LoadingImage from './LoadingImage'

interface Props {
  files: LibraryFeature[]
  onOpen: (index: number) => void
  // Admin-only re-tag (m-implement-view-only-admin-auth): undefined for a non-admin
  // (view-only) viewer, in which case the per-file "Re-tag location" button is hidden.
  onRetag?: (index: number) => void
}

// Pixel pointer-move below which a sheet-handle gesture counts as a tap (cycle snaps)
// rather than a drag (settle on the nearest snap).
const TAP_THRESHOLD_PX = 6

export default function FileListPanel({ files, onOpen, onRetag }: Props) {
  // Below 1024px the panel is a bottom sheet over a full-screen map; at desktop width it
  // stays the right rail (m-implement-mobile-responsive-ui).
  const mobile = useIsMobile()

  const empty = files.length === 0
  const place = files[0]?.properties.place_string ?? ''
  const date = files[0]?.properties.local_date ?? ''
  // The viewport-driven list can span many places/dates, so only label it with the
  // first file's place when every file shares that place (a co-located point or a
  // tight cluster), and only append the date when they also share that date;
  // otherwise show a neutral count header.
  const onePlace = !empty && files.every((f) => f.properties.place_string === place)
  const oneDate = onePlace && files.every((f) => f.properties.local_date === date)

  // Drag-to-resize (rail only): the panel is too narrow for long filenames at the
  // default width, so a left-edge handle lets the user widen it (clamped to a usable
  // range). Hidden in sheet mode (the sheet resizes vertically instead).
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

  // Bottom-sheet height as a fraction of the viewport height; rests at a SHEET_SNAPS
  // value. Starts at the middle "peek" snap so the list is visible without hiding the map.
  const [sheetFrac, setSheetFrac] = useState<number>(SHEET_SNAPS[1])
  const sheetDrag = useRef<{ startY: number; movedFar: boolean } | null>(null)
  // Live fraction kept in a ref so the pointerup handler reads the latest value (the
  // state closure captured at pointerdown would be stale after a drag). Mirrored in an
  // effect, NOT during render — a render-phase ref write trips react-hooks/refs (the same
  // reason Toolbar's pausedRef is synced in an effect); the handler reads it on the next
  // pointer event, by which point the commit + effect have run.
  const fracRef = useRef(sheetFrac)
  useEffect(() => {
    fracRef.current = sheetFrac
  }, [sheetFrac])

  const onHandleDown = (e: React.PointerEvent) => {
    sheetDrag.current = { startY: e.clientY, movedFar: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onHandleMove = (e: React.PointerEvent) => {
    const d = sheetDrag.current
    if (!d) return
    if (Math.abs(e.clientY - d.startY) > TAP_THRESHOLD_PX) d.movedFar = true
    // Fraction grows as the pointer moves UP from the bottom edge.
    const frac = clampFraction((window.innerHeight - e.clientY) / window.innerHeight)
    setSheetFrac(frac)
  }
  const onHandleUp = (e: React.PointerEvent) => {
    const d = sheetDrag.current
    sheetDrag.current = null
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    if (!d) return
    // A tap cycles to the next snap; a drag settles on the nearest snap.
    setSheetFrac(d.movedFar ? nearestSnap(fracRef.current) : cycleSnap(fracRef.current))
  }

  // Virtualize the thumbnail grid: a cluster can hold hundreds of files, and
  // mounting every thumb would fire one /api/thumb request per file at once. We
  // window by ROW (each row is `columns` files) so only viewport-visible rows
  // mount their LoadingImage. Columns track the panel's content width — the
  // (resizable) rail width on desktop, the live viewport width as a full-bleed
  // sheet on mobile. The ROW grid uses 1fr columns so cells always fit regardless
  // of the computed count (no horizontal overflow). The row-height estimate is the
  // cell width (square thumb, aspect-ratio:1) plus the filename + retag button;
  // `measureElement` corrects it from the real DOM height after first paint.
  const scrollRef = useRef<HTMLDivElement>(null)
  // Track the viewport width so the sheet's column count is responsive to rotation/resize.
  const [vw, setVw] = useState(() => (typeof window !== 'undefined' ? window.innerWidth : 768))
  useEffect(() => {
    if (!mobile) return
    const onResize = () => setVw(window.innerWidth)
    onResize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [mobile])

  // GAP_PX must match the .grid-row CSS gap so the column count and the cell-width
  // estimate agree; PAD_PX is the .grid padding on both sides.
  const GAP_PX = 6
  const PAD_PX = 8
  const panelWidth = mobile ? vw : width
  const columns = columnsForWidth(panelWidth, 120, GAP_PX)
  const rows = rowCount(files.length, columns)
  const cellWidth = (panelWidth - 2 * PAD_PX - (columns - 1) * GAP_PX) / columns
  const estRow = Math.max(80, cellWidth + 46)

  const virtualizer = useVirtualizer({
    count: rows,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estRow,
    overscan: 2,
  })

  const rootClass = `panel ${mobile ? 'panel--sheet' : 'panel--rail'}`
  const rootStyle = mobile ? { height: `${sheetFrac * 100}vh` } : { width }

  return (
    <div className={rootClass} style={rootStyle}>
      {mobile ? (
        <div
          className="sheet-handle"
          onPointerDown={onHandleDown}
          onPointerMove={onHandleMove}
          onPointerUp={onHandleUp}
          onPointerCancel={onHandleUp}
          title="Drag or tap to resize the list"
          role="button"
          aria-label="Resize file list"
        >
          <span className="sheet-grip" />
        </div>
      ) : (
        <div
          className="panel-resize"
          onPointerDown={onResizeDown}
          onPointerMove={onResizeMove}
          onPointerUp={onResizeUp}
          onPointerCancel={onResizeUp}
          title="Drag to resize"
        />
      )}
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
                          {f.properties.capture_kind === 'panorama' && f.properties.stitch_status === 'failed' && (
                            <span className="badge badge--stitch-failed" title="Panorama stitch failed">
                              ⚠ stitch
                            </span>
                          )}
                        </span>
                        <span className="thumb-name">
                          {f.properties.media_type === 'video' ? '▶ ' : ''}{f.properties.filename}
                        </span>
                      </button>
                      {onRetag && (
                        <button
                          className="retag"
                          onClick={() => onRetag(i)}
                          title="Re-tag this file's location by clicking the map"
                        >
                          ⌖ Re-tag location
                        </button>
                      )}
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
