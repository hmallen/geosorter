import { useState } from 'react'

// Flat zoomable hero for a non-360 stitched panorama
// (m-fix-panorama-projection-autodetect). A 180/wide/vertical pano is NOT a full
// sphere, so it is shown as a plain image (not wrapped onto PanoSphere): it fits the
// frame by default and toggles to 100% (native) on click, with the container scrolling
// so the user can pan a wide/tall result. Presentational, like PanoSphere.
export default function FlatHero({ src, alt }: { src: string; alt?: string }) {
  const [zoomed, setZoomed] = useState(false)
  return (
    <div className={`flat-hero${zoomed ? ' flat-hero--zoomed' : ''}`}>
      <img
        src={src}
        alt={alt}
        onClick={() => setZoomed((z) => !z)}
        title={zoomed ? 'Click to fit' : 'Click to zoom'}
      />
    </div>
  )
}
