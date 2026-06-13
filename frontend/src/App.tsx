import { useMemo, useState } from 'react'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import { useLibrary } from './useLibrary'
import { useRetagJob } from './useRetagJob'
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

  // After a re-organize / re-tag, close any open lightbox before reloading so it
  // can't keep rendering now-moved files (stale paths → broken media).
  function handleChanged() {
    setLightbox(null)
    reload()
  }

  const { retagging, placing, beginRetag, cancelRetag, pickLocation } = useRetagJob(handleChanged)

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
      <Toolbar onDone={handleChanged} stitchTargets={panoramaTargets} onReload={reload} />
      <MapView
        features={features}
        onMarkerClick={placing ? undefined : openInLightbox}
        onMapClick={placing ? pickLocation : undefined}
        onBoundsChange={setBounds}
      />
      {placing && (
        <div className="placement-banner">
          Click the map to set the new location
          <button onClick={cancelRetag}>Cancel</button>
        </div>
      )}
      {retagging && <div className="placement-banner">Re-filing…</div>}
      <FileListPanel
        files={panelFiles}
        onOpen={(i) => setLightbox({ files: panelFiles, index: i })}
        onRetag={(i) => {
          // panelFiles is volatile (viewport-driven); guard the deref in case a
          // click races a list shrink between paints.
          const file = panelFiles[i]
          if (file) beginRetag(file.properties.id)
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
