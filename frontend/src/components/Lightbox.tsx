import { useEffect, useState } from 'react'
import { fetchFrames, posterUrl, previewUrl, stitchUrl, thumbUrl, videoUrl } from '../api'
import { runStitch, type StitchState } from '../stitchJob'
import type { LibraryFeature } from '../types'

interface Props {
  files: LibraryFeature[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
}

export default function Lightbox({ files, index, onIndex, onClose }: Props) {
  const f = files[index]
  // Source-frame gallery: a hyperlapse render's frames (B10) or a panorama's tiles
  // (B12) are the only such entity, fetched on demand and shown as a thumbnail grid.
  const [frames, setFrames] = useState<string[] | null>(null)
  const [showFrames, setShowFrames] = useState(false)
  const [frameZoom, setFrameZoom] = useState<string | null>(null)

  // Offer the gallery only when frames were actually filed: a render with
  // retain_hyperlapse_frames=false (or a single-tile panorama) has frame_count 0 and
  // an empty /api/frames payload, so a "view source frames" button would dead-end.
  const kind = f?.properties.capture_kind
  const isFrameGallery =
    (kind === 'hyperlapse' || kind === 'panorama') && (f?.properties.frame_count ?? 0) > 0
  const fileId = f?.properties.id

  // Stitched panorama hero (B13): the ~7-min Hugin stitch is user-triggered. The
  // hero exists when the library's stitch_status is 'ok', or once an in-session run
  // completes 'ok'; otherwise the button offers to generate it (gallery stays).
  const isPanorama = kind === 'panorama'
  const [stitch, setStitch] = useState<StitchState | null>(null)
  const heroReady =
    isPanorama && (f?.properties.stitch_status === 'ok' || stitch?.status === 'ok')
  // Only offer to stitch when tiles were actually filed — a 0-tile panorama can't be
  // stitched (same gate as the frame gallery), so the button never starts a doomed job.
  const stitchable = isPanorama && (f?.properties.frame_count ?? 0) > 0
  const stitchBusy = stitch?.state === 'pending' || stitch?.state === 'running'

  // Reset the gallery + stitch state whenever the selected file changes.
  useEffect(() => {
    setShowFrames(false)
    setFrameZoom(null)
    setFrames(null)
    setStitch(null)
  }, [fileId])

  const generateStitch = () => {
    if (fileId === undefined) return
    setStitch({ state: 'pending', status: '', file_id: fileId, error: null })
    runStitch(fetch, fileId, { onProgress: setStitch })
      .then(setStitch)
      .catch(() =>
        setStitch({ state: 'error', status: 'failed', file_id: fileId, error: 'stitch failed' }),
      )
  }

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
        ) : heroReady ? (
          <img
            className="pano-hero"
            src={stitchUrl(f.properties.id)}
            alt={`${f.properties.filename} (stitched 360 panorama)`}
          />
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

        {stitchable && !heroReady && !frameZoom && (
          <div className="stitch-controls">
            {stitchBusy ? (
              <span className="stitch-status">Stitching panorama… (~7 min)</span>
            ) : stitch?.status === 'unavailable' ? (
              <span className="stitch-status">
                Panorama stitching unavailable (Hugin not installed).
              </span>
            ) : stitch?.status === 'failed' || stitch?.state === 'error' ? (
              <span className="stitch-status">Stitch failed — showing the tile gallery.</span>
            ) : (
              <button className="stitch-button" onClick={generateStitch}>
                ⊕ Generate stitched panorama
              </button>
            )}
          </div>
        )}

        {isFrameGallery && (
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

        {isFrameGallery && showFrames && !frameZoom && (
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
