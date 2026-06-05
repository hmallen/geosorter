import { useEffect, useRef } from 'react'
import { Viewer } from '@photo-sphere-viewer/core'
import '@photo-sphere-viewer/core/index.css'

// Immersive 360° viewer for the stitched equirectangular hero: drag to rotate,
// scroll to zoom. photo-sphere-viewer is framework-agnostic (wraps three.js), so we
// mount it imperatively on a ref div and destroy it on unmount / src change.
export default function PanoSphere({ src, alt }: { src: string; alt?: string }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = ref.current
    if (!container) return
    const viewer = new Viewer({
      container,
      panorama: src,
      navbar: ['zoom', 'move', 'fullscreen'],
      defaultZoomLvl: 0,
    })
    return () => viewer.destroy()
  }, [src])

  return <div className="pano-sphere" ref={ref} aria-label={alt} />
}
