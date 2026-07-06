import { lazy, Suspense, useEffect, useState } from 'react'
import { collageUrl, fetchFrames, posterUrl, previewUrl, stitchUrl, thumbUrl, videoUrl } from '../api'
import { captionInfo } from '../captionInfo'
import { resolvePanoViewer } from '../panoViewer'
import type { StitchState } from '../stitchJob'
import type { LibraryFeature } from '../types'
import FlatHero from './FlatHero'
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
  // Admin-only stitch trigger (m-implement-view-only-admin-auth): undefined for a
  // non-admin viewer, which hides the stitch CONTROLS. A previously-stitched hero is
  // still VIEWED by everyone; only generating/re-stitching is gated.
  onStartStitch?: (fileId: number, opts?: { force?: boolean; projection?: string }) => void
  // Flight-track overlay: offered for a video with an SRT sidecar (has_track).
  // App closes the lightbox and draws the path on the map.
  onShowTrack?: (f: LibraryFeature) => void
}

export default function Lightbox({
  files,
  index,
  onIndex,
  onClose,
  stitchByFile,
  onStartStitch,
  onShowTrack,
}: Props) {
  const f = files[index]
  // Source-frame gallery: a hyperlapse render's frames (B10) or a panorama's tiles
  // (B12) are the only such entity, fetched on demand and shown as a thumbnail grid.
  const [frames, setFrames] = useState<string[] | null>(null)
  const [showFrames, setShowFrames] = useState(false)
  const [frameZoom, setFrameZoom] = useState<string | null>(null)
  // Manual re-stitch projection override ('' = auto-detect, the default).
  const [projChoice, setProjChoice] = useState('')

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
  // Pick the hero viewer from the detected projection (m-fix-panorama-projection-
  // autodetect): prefer a non-empty in-session run value, else the library value (a
  // cache hit reports '' then backfills the library — see resolvePanoViewer). A non-360
  // 'flat' hero uses a flat zoomable image, else the 360 PanoSphere.
  const panoViewer = resolvePanoViewer(stitch?.projection, f?.properties.stitch_projection)
  // Only offer to stitch when tiles were actually filed — a 0-tile panorama can't be
  // stitched (same gate as the frame gallery), so the button never starts a doomed job.
  const stitchable = isPanorama && (f?.properties.frame_count ?? 0) > 0
  // Busy includes an untracked library-'pending' (a stitch started elsewhere/last
  // session that the auto-reattach below hasn't picked up yet) so the re-stitch button
  // stays hidden while a job is in flight — a click then can't be swallowed by the
  // submit dedup with its override silently dropped.
  const stitchBusy =
    stitch?.state === 'pending' ||
    stitch?.state === 'running' ||
    // An UNTRACKED library-'pending' (a stitch in flight that the auto-reattach below
    // hasn't picked up yet) counts as busy so the dedup can't drop an override; the
    // `!stitch` guard hands control to the live state once tracking starts, so a stale
    // snapshot 'pending' (the lightbox snapshots its `files`) can't pin the controls
    // busy after the live job has finished.
    (isPanorama && f?.properties.stitch_status === 'pending' && !stitch)
  // Cache-bust the hero ONLY after a re-stitch actually completes 'ok'. stitchUrl is a
  // stable URL, so a same-projection re-stitch would otherwise show the browser-cached
  // old JPEG. The job_id is set when the run STARTS (before the new JPEG is written), so
  // gating on done+ok (not merely job_id present) avoids re-caching the old image mid-run.
  const heroSrc =
    fileId !== undefined
      ? stitchUrl(fileId) +
        (stitch?.state === 'done' && stitch?.status === 'ok' && stitch?.job_id
          ? `?j=${stitch.job_id}`
          : '')
      : ''

  // Reset the gallery + projection choice whenever the selected file changes.
  useEffect(() => {
    setShowFrames(false)
    setFrameZoom(null)
    setFrames(null)
    setProjChoice('')
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
    if (fileId === undefined || !onStartStitch) return
    if (isPanorama && f?.properties.stitch_status === 'pending' && !stitch) {
      onStartStitch(fileId)
    }
  }, [fileId, isPanorama, f?.properties.stitch_status, stitch, onStartStitch])

  // Trigger a stitch with the chosen projection. force when a hero already exists
  // (a re-stitch must bypass the freshness cache) or when an explicit projection is
  // picked (so a stale cache doesn't shadow the override).
  const triggerStitch = () => {
    if (fileId === undefined || !onStartStitch) return
    onStartStitch(fileId, {
      force: heroReady || projChoice !== '',
      projection: projChoice || undefined,
    })
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

  // Keyboard operation: Escape closes, arrow keys page prev/next. Skipped while a
  // form control has focus (the projection <select> uses the arrow keys itself).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft') onIndex((index - 1 + files.length) % files.length)
      else if (e.key === 'ArrowRight') onIndex((index + 1) % files.length)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, files.length, onIndex, onClose])

  if (!f) return null
  const prev = () => onIndex((index - 1 + files.length) % files.length)
  const next = () => onIndex((index + 1) % files.length)

  return (
    <div
      className="lightbox"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={f.properties.filename}
    >
      <button className="lightbox-close" onClick={onClose} aria-label="Close">×</button>
      <div className="lightbox-body" onClick={(e) => e.stopPropagation()}>
        <div className="lightbox-caption">{captionInfo(f.properties)}</div>
        <div className="lightbox-media">
          {frameZoom ? (
            <LoadingImage key={frameZoom} src={previewUrl(frameZoom)} alt="source frame" />
          ) : heroReady ? (
            panoViewer === 'flat' ? (
              <FlatHero
                key={heroSrc}
                src={heroSrc}
                alt={`${f.properties.filename} (stitched panorama)`}
              />
            ) : (
              <Suspense fallback={<span className="img-spinner" aria-label="loading viewer" />}>
                <PanoSphere
                  key={heroSrc}
                  src={heroSrc}
                  alt={`${f.properties.filename} (stitched 360 panorama)`}
                />
              </Suspense>
            )
          ) : f.properties.media_type === 'video' ? (
            <video
              src={videoUrl(f.properties.path)}
              poster={posterUrl(f.properties.path)}
              controls
              autoPlay
            />
          ) : isPanorama ? (
            // Instant raw-tile collage placeholder: shown immediately while the
            // optional 360 stitch is absent/running, instead of a single tile.
            <LoadingImage
              key={`collage-${f.properties.id}`}
              src={collageUrl(f.properties.id)}
              alt={`${f.properties.filename} (raw-tile collage)`}
            />
          ) : (
            <LoadingImage
              key={f.properties.path}
              src={previewUrl(f.properties.path)}
              alt={f.properties.filename}
            />
          )}
        </div>

        {stitchable && !frameZoom && onStartStitch && (
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
            ) : (
              <>
                {(stitch?.status === 'failed' || stitch?.state === 'error') && (
                  <span className="stitch-status">
                    Stitch failed — adjust the projection and retry.
                  </span>
                )}
                <label className="stitch-projection">
                  Projection:
                  <select value={projChoice} onChange={(e) => setProjChoice(e.target.value)}>
                    <option value="">Auto-detect</option>
                    <option value="equirectangular">Equirectangular (360°)</option>
                    <option value="cylindrical">Cylindrical (wide)</option>
                    <option value="rectilinear">Rectilinear (flat)</option>
                  </select>
                </label>
                <button className="stitch-button" onClick={triggerStitch}>
                  {heroReady ? '⟳ Re-stitch' : '⊕ Generate stitched panorama'}
                </button>
              </>
            )}
          </div>
        )}

        {onShowTrack && f.properties.media_type === 'video' && f.properties.has_track && (
          <button
            className="frames-toggle"
            onClick={() => onShowTrack(f)}
            title="Close the viewer and draw this flight's GPS path on the map"
          >
            ✈ Show flight path on map
          </button>
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
        </div>
      </div>
    </div>
  )
}
