import { useMemo, useState } from 'react'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import QuarantinePanel from './components/QuarantinePanel'
import { useLibrary } from './useLibrary'
import { useRetagJob } from './useRetagJob'
import { useAssignLocation } from './useAssignLocation'
import { useQuarantine } from './useQuarantine'
import { useStitch } from './useStitch'
import { featuresInBounds } from './viewport'
import type { BBox } from './clusters'
import type { LibraryFeature } from './types'
import './App.css'

export default function App() {
  const { features, reload } = useLibrary()
  // Current map viewport bounds, lifted from MapView (null until the map's first
  // onLoad). The side panel always lists the captures inside these bounds.
  const [bounds, setBounds] = useState<BBox | null>(null)
  // The lightbox snapshots the file list it was opened against, so panning the map
  // (which live-updates panelFiles) can't shift its index onto a different file or
  // out of range while it is open.
  const [lightbox, setLightbox] = useState<{ files: LibraryFeature[]; index: number } | null>(null)
  // Panorama stitch tracking lives here (above the lightbox) so a ~7-min job's
  // progress survives the lightbox closing/reopening; reload on success so the hero
  // + stitch_status persist on the map and in the panel.
  const { byFile: stitchByFile, start: startStitch } = useStitch(reload)

  // Panel contents: every capture inside the current map viewport. A pure in-memory
  // filter over the already-loaded features — no /api refetch on pan/zoom. Memoized
  // so an unrelated re-render keeps a stable `files` identity for the virtualized
  // grid, and so a pan that doesn't move the settled bounds doesn't re-filter.
  const panelFiles = useMemo(
    () => (bounds ? featuresInBounds(features, bounds) : []),
    [features, bounds],
  )

  // Clicking a single map marker opens that capture in the lightbox against the
  // current viewport list (so prev/next walks the on-screen captures). A cluster
  // click is handled inside MapView by zooming in — it never reaches here.
  function openInLightbox(id: number) {
    const idx = panelFiles.findIndex((f) => f.properties.id === id)
    if (idx >= 0) {
      setLightbox({ files: panelFiles, index: idx })
      return
    }
    // The marker sits just outside the settled viewport bounds — open it solo.
    const f = features.find((ff) => ff.properties.id === id)
    if (f) setLightbox({ files: [f], index: 0 })
  }

  // No-GPS (quarantined) captures: the count badges the toolbar button and the list
  // feeds the No-GPS panel. Reloaded by handleChanged after an assign/organize/undo.
  const { items: quarantineItems, count: quarantineCount, reload: reloadQuarantine } =
    useQuarantine()
  const [showNoGps, setShowNoGps] = useState(false)

  // After a re-organize / re-tag / assign, close any open lightbox before reloading so
  // it can't keep rendering now-moved files (stale paths → broken media); also refresh
  // the no-GPS list (an assign removes items; an organize may add some).
  function handleChanged() {
    setLightbox(null)
    reload()
    reloadQuarantine()
  }

  const { retagging, placing: retagPlacing, beginRetag, cancelRetag, pickLocation: retagPick } =
    useRetagJob(handleChanged)
  // Bulk assign-location for no-GPS captures: placement mode (a map click sets the
  // location for the selected captures) coexists with re-tag placement.
  const {
    assigning,
    placing: assignPlacing,
    count: assignCount,
    beginAssign,
    cancelAssign,
    pickLocation: assignPick,
    assignToCoord,
  } = useAssignLocation(handleChanged)
  const placing = retagPlacing || assignPlacing
  // One map-click handler routed to whichever placement is active (only one can be).
  const onMapClick = retagPlacing ? retagPick : assignPlacing ? assignPick : undefined

  // Panoramas that still want a 360 stitch: a panorama with tiles whose stitch
  // hasn't succeeded yet. The toolbar's optional "Stitch all" button targets these.
  // Memoized on `features` so the per-pan re-renders (setBounds) don't rescan the
  // whole library every time.
  const panoramaTargets = useMemo(
    () =>
      features
        .filter(
          (f) =>
            f.properties.capture_kind === 'panorama' &&
            (f.properties.frame_count ?? 0) > 0 &&
            f.properties.stitch_status !== 'ok',
        )
        .map((f) => f.properties.id),
    [features],
  )

  return (
    <div className="app">
      <Toolbar
        onDone={handleChanged}
        stitchTargets={panoramaTargets}
        onReload={reload}
        onOpenNoGps={() => setShowNoGps(true)}
        noGpsCount={quarantineCount}
      />
      <MapView
        features={features}
        onMarkerClick={placing ? undefined : openInLightbox}
        onMapClick={onMapClick}
        onBoundsChange={setBounds}
      />
      {retagPlacing && (
        <div className="placement-banner">
          Click the map to set the new location
          <button onClick={cancelRetag}>Cancel</button>
        </div>
      )}
      {assignPlacing && (
        <div className="placement-banner">
          Click the map to set the location for {assignCount} no-GPS capture
          {assignCount === 1 ? '' : 's'}
          <button onClick={cancelAssign}>Cancel</button>
        </div>
      )}
      {retagging && <div className="placement-banner">Re-filing…</div>}
      {assigning && <div className="placement-banner">Assigning location…</div>}
      {showNoGps && (
        <QuarantinePanel
          items={quarantineItems}
          busy={assigning}
          onClose={() => setShowNoGps(false)}
          onPickOnMap={(ids) => {
            // The two placement modes are mutually exclusive: cancel a pending re-tag
            // so the next map click can't be routed to it (onMapClick prefers re-tag).
            cancelRetag()
            beginAssign(ids)
            setShowNoGps(false)
          }}
          onAssignToPlace={(ids, lat, lon) => assignToCoord(ids, lat, lon)}
        />
      )}
      <FileListPanel
        files={panelFiles}
        onOpen={(i) => setLightbox({ files: panelFiles, index: i })}
        onRetag={(i) => {
          // panelFiles is volatile (viewport-driven); guard the deref in case a
          // click races a list shrink between paints.
          const file = panelFiles[i]
          // Mutually exclusive with No-GPS assign placement (see onPickOnMap).
          if (file) {
            cancelAssign()
            beginRetag(file.properties.id)
          }
        }}
      />
      {lightbox && (
        <Lightbox
          files={lightbox.files}
          index={lightbox.index}
          onIndex={(i) => setLightbox((lb) => (lb ? { files: lb.files, index: i } : null))}
          onClose={() => setLightbox(null)}
          stitchByFile={stitchByFile}
          onStartStitch={startStitch}
        />
      )}
    </div>
  )
}
