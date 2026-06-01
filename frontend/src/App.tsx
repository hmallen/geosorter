import { useState } from 'react'
import type Supercluster from 'supercluster'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import { useLibrary } from './useLibrary'
import { selectionFor } from './selection'
import type { ClusterOrPoint } from './clusters'
import type { FeatureProps, LibraryFeature } from './types'
import './App.css'

export default function App() {
  const { features, reload } = useLibrary()
  const [selected, setSelected] = useState<LibraryFeature[]>([])
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)

  function handleSelect(index: Supercluster<FeatureProps>, item: ClusterOrPoint) {
    setSelected(selectionFor(index, item, features))
    setLightboxIndex(null)
  }

  // After a re-organize, clear any open selection/lightbox before reloading so
  // the panel can't keep rendering now-moved files (stale paths → broken media).
  function handleOrganized() {
    setSelected([])
    setLightboxIndex(null)
    reload()
  }

  return (
    <div className="app">
      <Toolbar onDone={handleOrganized} />
      <MapView features={features} onSelect={handleSelect} />
      {selected.length > 0 && (
        <FileListPanel
          files={selected}
          onOpen={(i) => setLightboxIndex(i)}
          onClose={() => setSelected([])}
        />
      )}
      {lightboxIndex !== null && (
        <Lightbox
          files={selected}
          index={lightboxIndex}
          onIndex={setLightboxIndex}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </div>
  )
}
