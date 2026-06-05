import { lazy, Suspense, useEffect, useState } from 'react'
import { fetchFrames, posterUrl, previewUrl, stitchUrl, thumbUrl, videoUrl } from '../api'
import type { StitchState } from '../stitchJob'
import type { LibraryFeature } from '../types'
import LoadingImage from './LoadingImage'

// Lazy so three.js + photo-sphere-viewer (~600 kB) only load when a stitched 360
// hero is actually viewed, keeping them out of the initial bundle.
const PanoSphere = lazy(() => import('./PanoSphere'))

interface Props {
  files: LibraryFeature[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
  // App-level stitch tracking (keyed by file_id) so progress survives reopen (B).
  stitchByFile: Record<number, StitchState>
  onStartStitch: (fileId: number) => void
}

export default function Lightbox({
  files,
  index,
  onIndex,
  onClose,
  stitchByFile,
  onStartStitch,
}: Props) {
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

  // Stitched panorama hero (B13): the ~7-min Hugin stitch is user-triggered, but its
  // job is tracked at the App level (useStitch) so closing/reopening the lightbox
  // never loses the live progress. The hero exists when the library's stitch_status
  // is 'ok', or once an in-session run completes 'ok'.
  const isPanorama = kind === 'panorama'
  const stitch = fileId !== undefined ? stitchByFile[fileId] : undefined
  const heroReady =
    isPanorama && (f?.properties.stitch_status === 'ok' || stitch?.status === 'ok')
  // Only offer to stitch when tiles were actually filed — a 0-tile panorama can't be
  // stitched (same gate as the frame gallery), so the button never starts a doomed job.
  const stitchable = isPanorama && (f?.properties.frame_count ?? 0) > 0
  const stitchBusy = stitch?.state === 'pending' || stitch?.state === 'running'

  // Reset the gallery whenever the selected file changes.
  useEffect(() => {
    setShowFrames(false)
    setFrameZoom(null)
    setFrames(null)
  }, [fileId])

  // Re-attach to an in-flight stitch on (re)open: if the library reports this
  // panorama as still 'pending' (e.g. after a page refresh) and we are not already
  // tracking it, kick the job — the server dedups to the running job and we resume
  // polling, so the user sees live progress instead of a stale Generate button. A
  // terminal 'failed'/'ok' status fails the guard, so this only resumes genuine
  // in-flight work. Re-fire is safe in any case: `onStartStitch` is referentially
  // stable (useStitch.start depends only on the stable `reload`) and useStitch's
  // inflight Set dedups synchronously, so this can never launch a second stitch.
  useEffect(() => {
    if (fileId === undefined) return
    if (isPanorama && f?.properties.stitch_status === 'pending' && !stitch) {
      onStartStitch(fileId)
    }
  }, [fileId, isPanorama, f?.properties.stitch_status, stitch, onStartStitch])

  const generateStitch = () => {
    if (fileId !== undefined) onStartStitch(fileId)
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
        <div className="lightbox-media">
          {frameZoom ? (
            <LoadingImage key={frameZoom} src={previewUrl(frameZoom)} alt="source frame" />
          ) : heroReady ? (
            <Suspense fallback={<span className="img-spinner" aria-label="loading viewer" />}>
              <PanoSphere
                src={stitchUrl(f.properties.id)}
                alt={`${f.properties.filename} (stitched 360 panorama)`}
              />
            </Suspense>
          ) : f.properties.media_type === 'video' ? (
            <video
              src={videoUrl(f.properties.path)}
              poster={posterUrl(f.properties.path)}
              controls
              autoPlay
            />
          ) : (
            <LoadingImage
              key={f.properties.path}
              src={previewUrl(f.properties.path)}
              alt={f.properties.filename}
            />
          )}
        </div>

        {stitchable && !heroReady && !frameZoom && (
          <div className="stitch-controls">
            {stitchBusy ? (
              <span className="stitch-status">
                Stitching panorama… step {stitch?.step ?? 0}/{stitch?.step_total ?? 6}
                {stitch?.step_name ? `: ${stitch.step_name}` : ''} (~7 min)
              </span>
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
                <LoadingImage key={path} src={thumbUrl(path)} alt={path} />
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
