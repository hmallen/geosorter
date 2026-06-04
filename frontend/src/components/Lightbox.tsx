import { useEffect, useState } from 'react'
import { fetchFrames, posterUrl, previewUrl, thumbUrl, videoUrl } from '../api'
import type { LibraryFeature } from '../types'

interface Props {
  files: LibraryFeature[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
}

export default function Lightbox({ files, index, onIndex, onClose }: Props) {
  const f = files[index]
  // Hyperlapse source-frame gallery (B10): the render is the only library entity,
  // so its 250-350 frames are fetched on demand and shown as a thumbnail grid.
  const [frames, setFrames] = useState<string[] | null>(null)
  const [showFrames, setShowFrames] = useState(false)
  const [frameZoom, setFrameZoom] = useState<string | null>(null)

  // Offer the frame gallery only when frames were actually filed: a render with
  // retain_hyperlapse_frames=false is a hyperlapse with frame_count 0 and no
  // /api/frames payload, so showing a "view source frames" button would dead-end.
  const isHyperlapse =
    f?.properties.capture_kind === 'hyperlapse' && (f?.properties.frame_count ?? 0) > 0
  const fileId = f?.properties.id

  // Reset the gallery whenever the selected file changes.
  useEffect(() => {
    setShowFrames(false)
    setFrameZoom(null)
    setFrames(null)
  }, [fileId])

  useEffect(() => {
    if (!showFrames || frames !== null || fileId === undefined) return
    let live = true
    fetchFrames(fileId)
      .then((fr) => live && setFrames(fr))
      .catch(() => live && setFrames([]))
    return () => {
      live = false
    }
  }, [showFrames, frames, fileId])

  if (!f) return null
  const prev = () => onIndex((index - 1 + files.length) % files.length)
  const next = () => onIndex((index + 1) % files.length)

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-body" onClick={(e) => e.stopPropagation()}>
        {frameZoom ? (
          <img src={previewUrl(frameZoom)} alt="source frame" />
        ) : f.properties.media_type === 'video' ? (
          <video
            src={videoUrl(f.properties.path)}
            poster={posterUrl(f.properties.path)}
            controls
            autoPlay
          />
        ) : (
          <img src={previewUrl(f.properties.path)} alt={f.properties.filename} />
        )}

        {isHyperlapse && (
          <button
            className="frames-toggle"
            onClick={() => (frameZoom ? setFrameZoom(null) : setShowFrames((s) => !s))}
          >
            {frameZoom
              ? '‹ Back to render'
              : showFrames
                ? 'Hide source frames'
                : `⊞ View ${f.properties.frame_count ?? ''} source frames`}
          </button>
        )}

        {isHyperlapse && showFrames && !frameZoom && (
          <div className="frames-grid">
            {frames === null && <span className="frames-status">Loading frames…</span>}
            {frames?.length === 0 && <span className="frames-status">No frames.</span>}
            {frames?.map((path) => (
              <button key={path} className="frame-thumb" onClick={() => setFrameZoom(path)}>
                <img src={thumbUrl(path)} alt={path} loading="lazy" />
              </button>
            ))}
          </div>
        )}

        <div className="lightbox-nav">
          <button onClick={prev} aria-label="Previous">‹</button>
          <span>{f.properties.filename}</span>
          <button onClick={next} aria-label="Next">›</button>
          <button onClick={onClose} aria-label="Close">×</button>
        </div>
      </div>
    </div>
  )
}
