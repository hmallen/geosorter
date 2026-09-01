import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { collageUrl, fetchFrames, posterUrl, previewUrl, stitchUrl, thumbUrl, videoUrl } from '../api'
import { captionInfo } from '../captionInfo'
import {
  PIP_ASPECT_RATIO,
  clampPipPosition,
  type PipPosition,
  type PipResizeCorner,
  resizePipFromCorner,
} from '../flightTrack'
import { resolvePanoViewer } from '../panoViewer'
import type { StitchState } from '../stitchJob'
import type { LibraryFeature, ViewerFlightContext } from '../types'
import { nextFlightAutoplayIndex, type PlaybackSeekRequest } from '../viewerPlayback'
import FlatHero from './FlatHero'
import LoadingImage from './LoadingImage'

// Lazy so three.js + photo-sphere-viewer (~600 kB) only load when a stitched 360
// hero is actually viewed, keeping them out of the initial bundle.
const PanoSphere = lazy(() => import('./PanoSphere'))

interface Props {
  files: LibraryFeature[]
  index: number
  flight: ViewerFlightContext | null
  onIndex: (index: number) => void
  onClose: () => void
  // App-level stitch tracking (keyed by file_id) so progress survives reopen (B).
  stitchByFile: Record<number, StitchState>
  // Admin-only stitch trigger (m-implement-view-only-admin-auth): undefined for a
  // non-admin viewer, which hides the stitch CONTROLS. A previously-stitched hero is
  // still VIEWED by everyone; only generating/re-stitching is gated.
  onStartStitch?: (fileId: number, opts?: { force?: boolean; projection?: string }) => void
  // Flight-track overlay: offered for a video with an SRT sidecar (has_track).
  // App fetches the path before switching this same video element into PiP mode.
  onShowTrack?: () => void
  // Video favorites: the CURRENT file's effective favorite state, computed in App
  // (effectiveFavorite over the live features + optimistic overrides — this
  // component stays dumb; its `files` are a stale snapshot).
  isFavorite?: boolean
  // Admin-gated heart toggle (like re-tag): undefined hides the control. Only
  // offered on videos per the feature request; `next` is the desired state.
  onToggleFavorite?: (id: number, next: boolean) => void
  trackMode: boolean
  trackActive: boolean
  trackLoading: boolean
  trackError: string | null
  trackWarning: string | null
  playbackSeek: PlaybackSeekRequest | null
  onPlaybackTime: (timeS: number) => void
  onExpand: () => void
}

export default function Lightbox({
  files,
  index,
  flight,
  onIndex,
  onClose,
  stitchByFile,
  onStartStitch,
  onShowTrack,
  isFavorite,
  onToggleFavorite,
  trackMode,
  trackActive,
  trackLoading,
  trackError,
  trackWarning,
  playbackSeek,
  onPlaybackTime,
  onExpand,
}: Props) {
  const f = files[index]
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const pipRef = useRef<HTMLDivElement | null>(null)
  const playbackCallback = useRef(onPlaybackTime)
  const resumeAfterTrackScrub = useRef<boolean | null>(null)
  const drag = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null)
  const resize = useRef<{
    pointerId: number
    corner: PipResizeCorner
    startX: number
    startY: number
    startLeft: number
    startTop: number
    startWidth: number
  } | null>(null)
  const [pipPosition, setPipPosition] = useState<PipPosition | null>(null)
  const [pipWidth, setPipWidth] = useState<number | null>(null)
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
  const pathAvailable = flight
    ? files.some((candidate) => candidate.properties.has_track)
    : Boolean(f?.properties.has_track)

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
  // Done during render (compare against the previous file id) rather than in an
  // effect: React re-renders before committing, so the old file's gallery state
  // never paints against the new file.
  const [prevFileId, setPrevFileId] = useState(fileId)
  if (fileId !== prevFileId) {
    setPrevFileId(fileId)
    setShowFrames(false)
    setFrameZoom(null)
    setFrames(null)
    setProjChoice('')
  }

  useEffect(() => {
    playbackCallback.current = onPlaybackTime
  }, [onPlaybackTime])

  // Map-marker drag requests are commands rather than mirrored playback state:
  // the token makes repeated seeks to the same timestamp observable without
  // feeding ordinary video timeupdate events back into this effect.
  useEffect(() => {
    const video = videoRef.current
    if (!video || !trackMode || !playbackSeek || playbackSeek.fileId !== fileId) return

    // MapLibre may emit dragstart and the first drag in one native callback,
    // allowing React to commit only the newest command. Treat the first command
    // observed in a scrub session as its start so pause/resume semantics survive
    // that batching (and even a start/end pair with no intermediate move).
    if (resumeAfterTrackScrub.current === null) {
      resumeAfterTrackScrub.current = !video.paused && !video.ended
      video.pause()
    }

    const duration = Number.isFinite(video.duration) ? video.duration : Infinity
    const target = Math.min(duration, Math.max(0, playbackSeek.timeS))
    video.currentTime = target
    playbackCallback.current(target)

    if (playbackSeek.phase === 'end') {
      const resume = resumeAfterTrackScrub.current
      resumeAfterTrackScrub.current = null
      if (resume) void video.play().catch(() => undefined)
    }
  }, [fileId, playbackSeek, trackMode])

  useEffect(() => {
    resumeAfterTrackScrub.current = null
  }, [fileId, trackMode])

  // Publish video.currentTime immediately for seeks/pause and at ~10 Hz while
  // playing. requestAnimationFrame is used only as a scheduler; the 100 ms gate
  // avoids driving the full app at display refresh rate.
  useEffect(() => {
    const video = videoRef.current
    if (!video || !trackMode) return
    let frame = 0
    let lastPublished = -Infinity

    const publish = () => playbackCallback.current(video.currentTime)
    const tick = (now: number) => {
      if (now - lastPublished >= 100) {
        lastPublished = now
        publish()
      }
      if (!video.paused && !video.ended) frame = requestAnimationFrame(tick)
      else frame = 0
    }
    const start = () => {
      publish()
      if (!frame) frame = requestAnimationFrame(tick)
    }
    const stop = () => {
      if (frame) cancelAnimationFrame(frame)
      frame = 0
      publish()
    }
    const seek = () => publish()

    video.addEventListener('play', start)
    video.addEventListener('pause', stop)
    video.addEventListener('ended', stop)
    video.addEventListener('seeking', seek)
    video.addEventListener('seeked', seek)
    video.addEventListener('timeupdate', seek)
    video.addEventListener('loadedmetadata', seek)
    publish()
    if (!video.paused && !video.ended) start()
    return () => {
      if (frame) cancelAnimationFrame(frame)
      video.removeEventListener('play', start)
      video.removeEventListener('pause', stop)
      video.removeEventListener('ended', stop)
      video.removeEventListener('seeking', seek)
      video.removeEventListener('seeked', seek)
      video.removeEventListener('timeupdate', seek)
      video.removeEventListener('loadedmetadata', seek)
    }
  }, [trackMode, fileId])

  // Initial PiP placement uses the map's available desktop width (left of the
  // resizable file rail); dragging itself is clamped to the full viewport.
  useLayoutEffect(() => {
    if (!trackMode) return
    const pip = pipRef.current
    if (!pip) return
    const place = () => {
      const rect = pip.getBoundingClientRect()
      const panel = window.innerWidth >= 1024
        ? document.querySelector<HTMLElement>('.panel')
        : null
      const availableRight = panel?.getBoundingClientRect().left ?? window.innerWidth
      const chipBottom =
        document.querySelector<HTMLElement>('.track-chip')?.getBoundingClientRect().bottom ?? 116
      setPipPosition((current) => {
        const candidate = current ?? {
          x: availableRight - rect.width - 16,
          y: chipBottom + 12,
        }
        return clampPipPosition(
          { ...candidate, y: Math.max(candidate.y, chipBottom + 12) },
          { width: rect.width, height: rect.height },
          { width: window.innerWidth, height: window.innerHeight },
        )
      })
    }
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [trackMode])

  const beginPipDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const pip = pipRef.current
    if (!pip) return
    const rect = pip.getBoundingClientRect()
    drag.current = {
      pointerId: e.pointerId,
      offsetX: e.clientX - rect.left,
      offsetY: e.clientY - rect.top,
    }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const movePip = (e: React.PointerEvent<HTMLDivElement>) => {
    const active = drag.current
    const pip = pipRef.current
    if (!active || active.pointerId !== e.pointerId || !pip) return
    const rect = pip.getBoundingClientRect()
    setPipPosition(
      clampPipPosition(
        { x: e.clientX - active.offsetX, y: e.clientY - active.offsetY },
        { width: rect.width, height: rect.height },
        { width: window.innerWidth, height: window.innerHeight },
      ),
    )
  }

  const endPipDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointerId === e.pointerId) drag.current = null
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
  }

  const beginPipResize = (
    corner: PipResizeCorner,
    e: React.PointerEvent<HTMLButtonElement>,
  ) => {
    const pip = pipRef.current
    if (!pip) return
    const rect = pip.getBoundingClientRect()
    resize.current = {
      pointerId: e.pointerId,
      corner,
      startX: e.clientX,
      startY: e.clientY,
      startLeft: rect.left,
      startTop: rect.top,
      startWidth: rect.width,
    }
    e.stopPropagation()
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const movePipResize = (e: React.PointerEvent<HTMLButtonElement>) => {
    const active = resize.current
    if (!active || active.pointerId !== e.pointerId) return
    const next = resizePipFromCorner(
      { x: active.startLeft, y: active.startTop, width: active.startWidth },
      active.corner,
      e.clientX - active.startX,
      e.clientY - active.startY,
      { width: window.innerWidth, height: window.innerHeight },
    )
    setPipPosition(next.position)
    setPipWidth(next.width)
  }

  const endPipResize = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (resize.current?.pointerId === e.pointerId) resize.current = null
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
  }

  const resizePipByKeyboard = (
    corner: PipResizeCorner,
    e: React.KeyboardEvent<HTMLButtonElement>,
  ) => {
    const pip = pipRef.current
    if (!pip) return
    const direction =
      e.key === 'ArrowDown'
        ? 1
        : e.key === 'ArrowUp'
          ? -1
          : corner === 'right'
            ? e.key === 'ArrowRight'
              ? 1
              : e.key === 'ArrowLeft'
                ? -1
                : 0
            : e.key === 'ArrowLeft'
              ? 1
              : e.key === 'ArrowRight'
                ? -1
                : 0
    if (!direction) return
    e.preventDefault()
    const rect = pip.getBoundingClientRect()
    const next = resizePipFromCorner(
      { x: rect.left, y: rect.top, width: rect.width },
      corner,
      0,
      (direction * 24) / PIP_ASPECT_RATIO,
      { width: window.innerWidth, height: window.innerHeight },
    )
    setPipPosition(next.position)
    setPipWidth(next.width)
  }

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
      else if (!trackMode && e.key === 'ArrowLeft') {
        onIndex((index - 1 + files.length) % files.length)
      } else if (!trackMode && e.key === 'ArrowRight') {
        onIndex((index + 1) % files.length)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, files.length, onIndex, onClose, trackMode])

  if (!f) return null
  const prev = () => onIndex((index - 1 + files.length) % files.length)
  const next = () => onIndex((index + 1) % files.length)
  const advanceAfterPlayback = () => {
    const nextIndex = nextFlightAutoplayIndex(index, files.length, flight !== null)
    if (nextIndex !== null) onIndex(nextIndex)
  }

  return (
    <div
      className={`lightbox${trackMode ? ' lightbox--pip' : ''}`}
      onClick={trackMode ? undefined : onClose}
      role="dialog"
      aria-modal={trackMode ? undefined : 'true'}
      aria-label={f.properties.filename}
    >
      {!trackMode && (
        <button className="lightbox-close" onClick={onClose} aria-label="Close">×</button>
      )}
      <div
        ref={pipRef}
        className="lightbox-body"
        style={
          trackMode
            ? {
                ...(pipPosition ? { left: pipPosition.x, top: pipPosition.y } : {}),
                ...(pipWidth !== null ? { width: pipWidth } : {}),
              }
            : undefined
        }
        onClick={(e) => e.stopPropagation()}
      >
        {trackMode && (
          <div
            className="pip-header"
            onPointerDown={beginPipDrag}
            onPointerMove={movePip}
            onPointerUp={endPipDrag}
            onPointerCancel={endPipDrag}
          >
            <span className="pip-title" title={f.properties.filename}>
              {f.properties.filename}
            </span>
            <div className="pip-actions">
              <button
                type="button"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={onExpand}
                aria-label="Expand video"
                title="Expand video"
              >
                ⛶
              </button>
              <button
                type="button"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={onClose}
                aria-label="Close video and flight track"
                title="Close video and flight track"
              >
                ×
              </button>
            </div>
          </div>
        )}
        {!trackMode && (
          <div className="lightbox-caption">{captionInfo(f.properties)}</div>
        )}
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
              key={f.properties.id}
              ref={videoRef}
              src={videoUrl(f.properties.path)}
              poster={posterUrl(f.properties.path)}
              controls
              autoPlay
              playsInline
              onEnded={advanceAfterPlayback}
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
        {trackMode && (
          <div className="pip-resize-bar">
            {(['left', 'right'] as const).map((corner) => (
              <button
                key={corner}
                type="button"
                className={`pip-resize-handle pip-resize-handle--${corner}`}
                aria-label={`Resize video window from bottom ${corner}`}
                title={`Drag bottom ${corner} corner to resize video window`}
                onPointerDown={(e) => beginPipResize(corner, e)}
                onPointerMove={movePipResize}
                onPointerUp={endPipResize}
                onPointerCancel={endPipResize}
                onKeyDown={(e) => resizePipByKeyboard(corner, e)}
              />
            ))}
          </div>
        )}

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

        {/* Video actions row: favorite toggle + the flight-path entry point. Both
            are chrome, so the whole row is hidden once the viewer is docked as a
            PiP over the map (trackMode). */}
        {!trackMode &&
          f.properties.media_type === 'video' &&
          (onToggleFavorite || (onShowTrack && pathAvailable)) && (
          <div className="lightbox-actions">
            {onToggleFavorite && (
              <button
                className={`frames-toggle fav-toggle${isFavorite ? ' fav-toggle--on' : ''}`}
                onClick={() => onToggleFavorite(f.properties.id, !isFavorite)}
                aria-pressed={!!isFavorite}
                title={isFavorite ? 'Remove from favorites' : 'Add to favorites'}
              >
                {isFavorite ? '♥ Favorited' : '♡ Favorite'}
              </button>
            )}
            {onShowTrack && pathAvailable && (
              <button
                className="frames-toggle"
                onClick={onShowTrack}
                disabled={trackLoading}
                title={flight
                  ? 'Show every available path in this flight on the map'
                  : 'Show this video over its synchronized GPS flight path'}
              >
                {trackLoading
                  ? flight ? 'Loading flight paths…' : 'Loading flight path…'
                  : trackActive
                    ? flight ? '✈ Return to flight paths' : '✈ Return to flight map'
                    : flight ? '✈ Show flight paths on map' : '✈ Show flight path on map'}
              </button>
            )}
            {trackError && (
              <div className="track-inline-error" role="alert">{trackError}</div>
            )}
            {trackWarning && !trackError && (
              <div className="track-inline-warning">{trackWarning}</div>
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

        {!trackMode && (
          <div className="lightbox-nav">
            <button onClick={prev} aria-label="Previous">‹</button>
            <span>{f.properties.filename}</span>
            <button onClick={next} aria-label="Next">›</button>
          </div>
        )}
      </div>
    </div>
  )
}
