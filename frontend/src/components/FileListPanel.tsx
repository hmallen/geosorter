import { useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { listThumb } from '../api'
import { columnsForWidth } from '../gridWindow'
import { groupFeatures, buildRowModel, type Granularity, type SortDir } from '../dateGroups'
import {
  buildFlightRowModel,
  changeGranularity,
  flightHeaderRowIndex,
  groupFlightsByDate,
  initialGroupingFilterState,
  selectionForFlight,
  setFlightSubgroups,
  toggleGroupingCategory,
  type FlightCatalog,
  type PanelFlightGroup,
} from '../flightGroups'
import { filterByCategories, MEDIA_CATEGORIES, type MediaCategory } from '../mediaFilter'
import { useIsMobile } from '../useMediaQuery'
import { clampFraction, nearestSnap, cycleSnap, SHEET_SNAPS } from '../sheet'
import type { LibraryFeature, ViewerFlightContext, ViewerSelection } from '../types'
import LoadingImage from './LoadingImage'

interface Props {
  files: LibraryFeature[]
  // Flight assignments are inferred from the whole app-filtered library (before map
  // bounds), so a viewport subset cannot split or renumber a flight while panning.
  flightCatalog: FlightCatalog
  // The panel groups + filters its files, so the displayed order differs from the raw
  // viewport list. onOpen receives the EXACT displayed-ordered list it opened against
  // (so the lightbox prev/next walks what the user sees) plus the clicked index.
  onOpen: (selection: ViewerSelection) => void
  // Present only while a flight is docked over the map. The panel keeps this context
  // visible even when the user has panned or is not currently using flight grouping.
  activeFlight?: ViewerFlightContext | null
  activeFileId?: number | null
  // Refit the map to the loaded paths before scrolling when the user has panned away.
  onRevealActiveFlight?: () => void
  // Admin-only re-tag (m-implement-view-only-admin-auth): undefined for a non-admin
  // (view-only) viewer, in which case the per-file "Re-tag location" button is hidden.
  // Receives the clicked feature directly (the index would be ambiguous once filtered).
  onRetag?: (file: LibraryFeature) => void
}

// Pixel pointer-move below which a sheet-handle gesture counts as a tap (cycle snaps)
// rather than a drag (settle on the nearest snap).
const TAP_THRESHOLD_PX = 6

// Human labels for the media-filter chips.
const CATEGORY_LABELS: Record<MediaCategory, string> = {
  photo: 'Photos',
  video: 'Videos',
  panorama: 'Panoramas',
  hyperlapse: 'Hyperlapse',
}

// Estimated height of a group-header row (corrected by measureElement after paint).
const HEADER_PX = 34
const FLIGHT_HEADER_PX = 54

export default function FileListPanel({
  files,
  flightCatalog,
  onOpen,
  activeFlight,
  activeFileId,
  onRevealActiveFlight,
  onRetag,
}: Props) {
  // Below 1024px the panel is a bottom sheet over a full-screen map; at desktop width it
  // stays the right rail (m-implement-mobile-responsive-ui).
  const mobile = useIsMobile()

  // Grouping + media categories remain panel-local. Enabling flight subgroups snapshots
  // the current category selection and locks the panel to ordinary videos; disabling
  // restores that exact snapshot (see flightGroups.setFlightSubgroups).
  const [grouping, setGrouping] = useState(initialGroupingFilterState)
  // Date sort direction: descending (newest-first) by default, matching prior behavior.
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const toggleCategory = (c: MediaCategory) =>
    setGrouping((prev) => toggleGroupingCategory(prev, c))

  // Apply the media filter, then either keep the ordinary flat date buckets or nest the
  // viewport subset into catalog-backed flights. Non-flight lightboxes still walk the
  // flattened display order; a flight click instead opens that flight's full membership.
  const visibleFiles = useMemo(
    () => filterByCategories(files, grouping.enabled),
    [files, grouping.enabled],
  )
  const groups = useMemo(
    () => grouping.subgroupFlights
      ? []
      : groupFeatures(visibleFiles, grouping.granularity, sortDir),
    [visibleFiles, grouping.subgroupFlights, grouping.granularity, sortDir],
  )
  const flightDateGroups = useMemo(
    () => grouping.subgroupFlights
      ? groupFlightsByDate(visibleFiles, flightCatalog, grouping.granularity, sortDir)
      : [],
    [visibleFiles, flightCatalog, grouping.subgroupFlights, grouping.granularity, sortDir],
  )
  const orderedFiles = useMemo(() => groups.flatMap((g) => g.files), [groups])
  // id→display-index lookup for the onOpen call. Assumes feature ids are unique within
  // the viewport (true for /api/library — each capture is one feature); a duplicate id
  // would collapse to its last occurrence.
  const idxById = useMemo(() => {
    const m = new Map<number, number>()
    orderedFiles.forEach((f, i) => m.set(f.properties.id, i))
    return m
  }, [orderedFiles])
  const flightByKey = useMemo(() => {
    const map = new Map<string, PanelFlightGroup>()
    for (const dateGroup of flightDateGroups) {
      for (const flight of dateGroup.flights) map.set(flight.key, flight)
    }
    return map
  }, [flightDateGroups])

  const empty = visibleFiles.length === 0
  const filteredOut = empty && files.length > 0
  const place = visibleFiles[0]?.properties.place_string ?? ''
  const date = visibleFiles[0]?.properties.local_date ?? ''
  // The viewport-driven list can span many places/dates, so only label it with the
  // first file's place when every visible file shares that place (a co-located point or a
  // tight cluster), and only append the date when they also share that date.
  const onePlace = !empty && visibleFiles.every((f) => f.properties.place_string === place)
  const oneDate = onePlace && visibleFiles.every((f) => f.properties.local_date === date)

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

  // Virtualize a flat row model: group HEADER rows interleaved with THUMB rows (each
  // thumb row is up to `columns` files of one group, built by dateGroups.buildRowModel).
  // Only viewport-visible thumb rows mount a LoadingImage, so a large cluster still
  // issues only a bounded set of /api/thumb requests. Columns track the panel's content
  // width — the (resizable) rail width on desktop, the live viewport width as a full-bleed
  // sheet on mobile. measureElement corrects each row's height (headers are short) from
  // the real DOM after first paint.
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
  const cellWidth = (panelWidth - 2 * PAD_PX - (columns - 1) * GAP_PX) / columns
  const estRow = Math.max(80, cellWidth + 46)

  const rowItems = useMemo(
    () => grouping.subgroupFlights
      ? buildFlightRowModel(flightDateGroups, columns)
      : buildRowModel(groups, columns),
    [grouping.subgroupFlights, flightDateGroups, groups, columns],
  )

  const virtualizer = useVirtualizer({
    count: rowItems.length,
    getScrollElement: () => scrollRef.current,
    // Key each row by its CONTENT identity, not its index. The virtualizer caches
    // measured heights by item key; rows now have two very different heights (short
    // header vs tall thumb row), so without a stable key a header that lands at an
    // index previously occupied by a thumb row would inherit the thumb row's cached
    // height for a frame when the granularity/filter/column-count changes. The row
    // keys embed the bucket key + column-dependent offset, so they change whenever the
    // row's content does (incl. a column change reshuffling thumb-row boundaries).
    getItemKey: (index) => rowItems[index]?.key ?? index,
    estimateSize: (index) => {
      const kind = rowItems[index]?.kind
      if (kind === 'date-header') return HEADER_PX
      if (kind === 'flight-header') return FLIGHT_HEADER_PX
      return estRow
    },
    overscan: 4,
  })

  // A jump can first need to enable flight grouping and/or refit the map. Keep the
  // semantic flight key pending until the corresponding virtual row exists, then let
  // the virtualizer reveal it without depending on any DOM node being mounted.
  const pendingFlightJump = useRef<string | null>(null)
  const jumpToActiveFlight = () => {
    if (!activeFlight) return
    pendingFlightJump.current = activeFlight.key
    onRevealActiveFlight?.()
    if (!grouping.subgroupFlights) {
      setGrouping((prev) => setFlightSubgroups(prev, true))
      return
    }
    const rowIndex = flightHeaderRowIndex(rowItems, activeFlight.key)
    if (rowIndex >= 0) {
      virtualizer.scrollToIndex(rowIndex, { align: 'start' })
      pendingFlightJump.current = null
    }
  }

  // When the displayed set changes (grouping toggle or media filter), reset the
  // scroll to the top so filtering down to fewer rows doesn't strand the user scrolled
  // past the (now shorter) content.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 })
  }, [grouping.granularity, grouping.subgroupFlights, grouping.enabled, sortDir])

  // Run after the general filter-reset effect above so a jump that had to enable
  // flight grouping wins over that reset even when the target flight is not first.
  useEffect(() => {
    const flightKey = pendingFlightJump.current
    if (!flightKey) return
    const rowIndex = flightHeaderRowIndex(rowItems, flightKey)
    if (rowIndex < 0) return
    virtualizer.scrollToIndex(rowIndex, { align: 'start' })
    pendingFlightJump.current = null
  }, [rowItems, virtualizer])

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
              <small>{oneDate ? `${date} · ` : ''}{visibleFiles.length} file(s)</small>
            </>
          ) : (
            <>
              <strong>In view</strong>
              <br />
              <small>{visibleFiles.length} file(s)</small>
            </>
          )}
        </div>
      </div>
      {activeFlight && (
        <button
          className="current-flight"
          onClick={jumpToActiveFlight}
          aria-label={`Jump to currently playing flight: ${activeFlight.label}`}
        >
          <span className="current-flight__copy">
            <strong>Currently playing</strong>
            <small>{activeFlight.label}</small>
          </span>
          <span className="current-flight__action">Jump to flight</span>
        </button>
      )}
      <div className="panel-controls">
        <div className="seg" role="group" aria-label="Group by">
          {(['day', 'month', 'year'] as Granularity[]).map((g) => (
            <button
              key={g}
              className={`seg-btn${grouping.granularity === g ? ' seg-btn--active' : ''}`}
              onClick={() => setGrouping((prev) => changeGranularity(prev, g))}
              aria-pressed={grouping.granularity === g}
            >
              {g === 'day' ? 'Day' : g === 'month' ? 'Month' : 'Year'}
            </button>
          ))}
        </div>
        <label className="flight-subgroup-toggle">
          <input
            type="checkbox"
            checked={grouping.subgroupFlights}
            onChange={(e) =>
              setGrouping((prev) => setFlightSubgroups(prev, e.target.checked))
            }
          />
          <span>Subgroup videos by flight</span>
        </label>
        <button
          className="seg-btn sort-toggle"
          onClick={() => setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'))}
          aria-pressed={sortDir === 'asc'}
          title="Toggle date sort order"
        >
          {sortDir === 'desc' ? 'Newest first ↓' : 'Oldest first ↑'}
        </button>
        <div className="chips" role="group" aria-label="Filter by media type">
          {MEDIA_CATEGORIES.map((c) => (
            <button
              key={c}
              className={`chip${grouping.enabled.has(c) ? ' chip--on' : ''}`}
              onClick={() => toggleCategory(c)}
              aria-pressed={grouping.enabled.has(c)}
              disabled={grouping.subgroupFlights}
              title={grouping.subgroupFlights ? 'Flight grouping shows ordinary videos only' : undefined}
            >
              {CATEGORY_LABELS[c]}
            </button>
          ))}
        </div>
      </div>
      {empty && (
        <div className="panel-empty">
          {filteredOut
            ? 'No captures match the current filters'
            : 'No captures in this view — pan or zoom the map'}
        </div>
      )}
      <div className="grid" ref={scrollRef}>
        <div className="grid-sizer" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((vrow) => {
            const item = rowItems[vrow.index]
            if (!item) return null
            if (item.kind === 'date-header') {
              return (
                <div
                  key={item.key}
                  data-index={vrow.index}
                  ref={virtualizer.measureElement}
                  className="group-header"
                  style={{ transform: `translateY(${vrow.start}px)` }}
                >
                  {item.label}
                </div>
              )
            }
            if (item.kind === 'flight-header') {
              const active = item.flightKey === activeFlight?.key
              const count = item.visibleCount === item.totalCount
                ? `${item.totalCount} video${item.totalCount === 1 ? '' : 's'}`
                : `${item.visibleCount} of ${item.totalCount} videos in view`
              return (
                <div
                  key={item.key}
                  data-index={vrow.index}
                  ref={virtualizer.measureElement}
                  className={`flight-group-header${active ? ' flight-group-header--active' : ''}`}
                  style={{ transform: `translateY(${vrow.start}px)` }}
                >
                  <strong>{item.label}</strong>
                  <small>{count}</small>
                </div>
              )
            }
            const flight = item.flightKey ? flightByKey.get(item.flightKey) : undefined
            const activeFlightRow = item.flightKey === activeFlight?.key
            return (
              <div
                key={item.key}
                data-index={vrow.index}
                ref={virtualizer.measureElement}
                className={`grid-row${item.flightPosition
                  ? ` grid-row--flight grid-row--flight-${item.flightPosition}`
                  : ''}${activeFlightRow ? ' grid-row--active-flight' : ''}`}
                style={{
                  transform: `translateY(${vrow.start}px)`,
                  gridTemplateColumns: `repeat(${columns}, 1fr)`,
                }}
              >
                {item.files.map((f) => (
                  <div
                    key={f.properties.id}
                    className={`thumb${f.properties.id === activeFileId ? ' thumb--active' : ''}`}
                  >
                    <button
                      className="thumb-open"
                      onClick={() => {
                        if (flight) onOpen(selectionForFlight(flight, f.properties.id))
                        else {
                          onOpen({
                            files: orderedFiles,
                            index: idxById.get(f.properties.id) ?? 0,
                            flight: null,
                          })
                        }
                      }}
                    >
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
                        onClick={() => onRetag(f)}
                        title="Re-tag this file's location by clicking the map"
                      >
                        ⌖ Re-tag location
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
