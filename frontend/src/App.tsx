import { useMemo, useState } from 'react'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import QuarantinePanel from './components/QuarantinePanel'
import LocationPanel from './components/LocationPanel'
import StitchPanel from './components/StitchPanel'
import { buildPlaces } from './locationFilter'
import { useLibrary } from './useLibrary'
import { useRetagJob } from './useRetagJob'
import { useAssignLocation } from './useAssignLocation'
import { useQuarantine } from './useQuarantine'
import { useStitch } from './useStitch'
import { useStitchAll } from './useStitchAll'
import { useAuthContext } from './useAuth'
import { featuresInBounds } from './viewport'
import type { BBox } from './clusters'
import type { LibraryFeature, QuarantineItem } from './types'
import './App.css'

// Build a minimal LibraryFeature from a no-GPS QuarantineItem so its media can be
// previewed in the shared Lightbox. Quarantined captures carry no coordinate, so the
// geometry is a placeholder [0,0] (the Lightbox reads only `properties`, never geometry).
// capture_kind/frame_count are deliberately nulled: the preview is VIEW-ONLY, so the
// Lightbox must not surface the panorama stitch / source-frame controls (whose endpoints
// gate on capture_kind, not status) and let the user start a multi-minute stitch on a
// still-quarantined capture that an assign is about to relocate.
function quarantineToFeature(item: QuarantineItem): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: {
      id: item.id,
      filename: item.filename,
      place_string: null,
      local_date: item.date,
      capture_ts_local: null,
      media_type: item.media_type,
      codec: null,
      gps_source: 'none',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      path: item.path,
    },
  }
}

export default function App() {
  // Admin gate (m-implement-view-only-admin-auth): isAdmin is true when no password
  // is configured (open app) or the user has logged in. The management controls below
  // are passed down only when isAdmin, so a view-only viewer never sees them.
  const { isAdmin } = useAuthContext()
  const { features, reload, loading: libraryLoading, error: libraryError } = useLibrary()
  // Current map viewport bounds, lifted from MapView (null until the map's first
  // onLoad). The side panel always lists the captures inside these bounds.
  const [bounds, setBounds] = useState<BBox | null>(null)
  // The lightbox snapshots the file list it was opened against, so panning the map
  // (which live-updates panelFiles) can't shift its index onto a different file or
  // out of range while it is open.
  const [lightbox, setLightbox] = useState<{ files: LibraryFeature[]; index: number } | null>(null)
  // Location-filter panel: the distinct-place list (derived client-side) + the
  // panel open flag + the imperative map-fit target. `nonce` makes each pick a
  // distinct value so re-picking the same place re-fires MapView's fitBounds.
  const [showLocations, setShowLocations] = useState(false)
  // Unstitched-panorama panel: a library-wide list of which panorama sets still want a
  // 360 stitch (the toolbar shows only the count).
  const [showStitch, setShowStitch] = useState(false)
  const [flyTo, setFlyTo] = useState<{ bbox: BBox; nonce: number } | null>(null)
  const places = useMemo(() => buildPlaces(features), [features])
  // Panorama stitch tracking lives here (above the lightbox) so a ~7-min job's
  // progress survives the lightbox closing/reopening; reload on success so the hero
  // + stitch_status persist on the map and in the panel.
  const { byFile: stitchByFile, start: startStitch } = useStitch(reload)
  // The "Stitch all panoramas" batch action lives here (not in the unmounting
  // StitchPanel) so an in-flight run survives the panel closing/reopening; reload
  // the library on completion so finished panoramas drop out of the target set.
  const stitchAll = useStitchAll(reload)

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
    assign,
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
  // hasn't succeeded yet. The toolbar's "Stitch all" button + the StitchPanel target
  // these. Memoized on `features` so the per-pan re-renders (setBounds) don't rescan
  // the whole library every time. The full features feed the StitchPanel (it shows a
  // thumbnail + label); the ids feed the toolbar's stitch-all.
  const panoramaTargetFeatures = useMemo(
    () =>
      features.filter(
        (f) =>
          f.properties.capture_kind === 'panorama' &&
          (f.properties.frame_count ?? 0) > 0 &&
          f.properties.stitch_status !== 'ok',
      ),
    [features],
  )
  const panoramaTargets = useMemo(
    () => panoramaTargetFeatures.map((f) => f.properties.id),
    [panoramaTargetFeatures],
  )

  return (
    <div className="app">
      <Toolbar
        admin={isAdmin}
        onDone={handleChanged}
        stitchTargets={panoramaTargets}
        onOpenNoGps={() => setShowNoGps((v) => !v)}
        noGpsCount={quarantineCount}
        onOpenLocations={() => setShowLocations((v) => !v)}
        onOpenStitch={() => setShowStitch((v) => !v)}
      />
      <MapView
        features={features}
        onMarkerClick={placing ? undefined : openInLightbox}
        onMapClick={onMapClick}
        onBoundsChange={setBounds}
        flyTo={flyTo ?? undefined}
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
      {/* Initial-load status: without it a failed/slow /api/library read is
          indistinguishable from an empty library (blank world map). Shown only
          before the first successful load — reloads revalidate silently. */}
      {features.length === 0 && libraryLoading && !libraryError && (
        <div className="library-status">
          <span className="spinner" aria-hidden="true" /> Loading library…
        </div>
      )}
      {features.length === 0 && libraryError && !libraryLoading && (
        <div className="library-status library-status--error" role="alert">
          Couldn't load the library.
          <button onClick={reload}>Retry</button>
        </div>
      )}
      {assigning && (
        <div className="placement-banner">
          {assign && assign.total > 0 ? (
            <>
              Assigning location… {assign.processed} of {assign.total}
              <progress value={Math.min(assign.processed, assign.total)} max={assign.total} />
            </>
          ) : (
            'Assigning location…'
          )}
        </div>
      )}
      {showNoGps && (
        <QuarantinePanel
          items={quarantineItems}
          busy={assigning}
          onClose={() => setShowNoGps(false)}
          onView={(item) => {
            // Open the WHOLE no-GPS list (as view-only features) at the clicked item, so
            // the lightbox prev/next walks every quarantined capture instead of dead-ending.
            const feats = quarantineItems.map(quarantineToFeature)
            const idx = quarantineItems.findIndex((q) => q.id === item.id)
            setLightbox({ files: feats, index: idx < 0 ? 0 : idx })
          }}
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
      {showLocations && (
        <LocationPanel
          places={places}
          onClose={() => setShowLocations(false)}
          onPick={(bbox) => {
            setFlyTo((p) => ({ bbox, nonce: (p?.nonce ?? 0) + 1 }))
            setShowLocations(false)
          }}
        />
      )}
      {showStitch && (
        <StitchPanel
          panoramas={panoramaTargetFeatures}
          stitchByFile={stitchByFile}
          onStartStitch={isAdmin ? startStitch : undefined}
          onStitchAll={isAdmin ? () => stitchAll.start(panoramaTargets) : undefined}
          onCancelStitchAll={stitchAll.cancel}
          stitchAllRunning={stitchAll.running}
          stitchAllProgress={stitchAll.progress}
          stitchAllResult={stitchAll.result}
          onView={(f) => {
            // Open the unstitched-panorama list at this capture so prev/next walks them.
            const idx = panoramaTargetFeatures.findIndex(
              (p) => p.properties.id === f.properties.id,
            )
            setLightbox({ files: panoramaTargetFeatures, index: idx < 0 ? 0 : idx })
          }}
          onClose={() => setShowStitch(false)}
        />
      )}
      <FileListPanel
        files={panelFiles}
        onOpen={(files, i) => setLightbox({ files, index: i })}
        onRetag={
          isAdmin
            ? (file) => {
                // Mutually exclusive with No-GPS assign placement (see onPickOnMap).
                cancelAssign()
                beginRetag(file.properties.id)
              }
            : undefined
        }
      />
      {lightbox && (
        <Lightbox
          files={lightbox.files}
          index={lightbox.index}
          onIndex={(i) => setLightbox((lb) => (lb ? { files: lb.files, index: i } : null))}
          onClose={() => setLightbox(null)}
          stitchByFile={stitchByFile}
          onStartStitch={isAdmin ? startStitch : undefined}
        />
      )}
    </div>
  )
}
