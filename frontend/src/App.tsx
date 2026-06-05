import { useState } from 'react'
import type Supercluster from 'supercluster'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import { useLibrary } from './useLibrary'
import { useRetagJob } from './useRetagJob'
import { useStitch } from './useStitch'
import { selectionFor } from './selection'
import type { ClusterOrPoint } from './clusters'
import type { FeatureProps, LibraryFeature } from './types'
import './App.css'

export default function App() {
  const { features, reload } = useLibrary()
  const [selected, setSelected] = useState<LibraryFeature[]>([])
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
  // Panorama stitch tracking lives here (above the lightbox) so a ~7-min job's
  // progress survives the lightbox closing/reopening; reload on success so the hero
  // + stitch_status persist on the map and in future selections.
  const { byFile: stitchByFile, start: startStitch } = useStitch(reload)

  function handleSelect(index: Supercluster<FeatureProps>, item: ClusterOrPoint) {
    setSelected(selectionFor(index, item, features))
    setLightboxIndex(null)
  }

  // After a re-organize / re-tag, clear any open selection/lightbox before
  // reloading so the panel can't keep rendering now-moved files (stale paths →
  // broken media).
  function handleChanged() {
    setSelected([])
    setLightboxIndex(null)
    reload()
  }

  const { retagging, placing, beginRetag, cancelRetag, pickLocation } = useRetagJob(handleChanged)

  return (
    <div className="app">
      <Toolbar onDone={handleChanged} />
      <MapView
        features={features}
        onSelect={handleSelect}
        onMapClick={placing ? pickLocation : undefined}
      />
      {placing && (
        <div className="placement-banner">
          Click the map to set the new location
          <button onClick={cancelRetag}>Cancel</button>
        </div>
      )}
      {retagging && <div className="placement-banner">Re-filing…</div>}
      {selected.length > 0 && (
        <FileListPanel
          files={selected}
          onOpen={(i) => setLightboxIndex(i)}
          onRetag={(i) => beginRetag(selected[i].properties.id)}
          onClose={() => setSelected([])}
        />
      )}
      {lightboxIndex !== null && (
        <Lightbox
          files={selected}
          index={lightboxIndex}
          onIndex={setLightboxIndex}
          onClose={() => setLightboxIndex(null)}
          stitchByFile={stitchByFile}
          onStartStitch={startStitch}
        />
      )}
    </div>
  )
}
