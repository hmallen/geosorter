import { useEffect, useRef, useState, type RefObject } from 'react'
import { posterUrl, videoUrl } from '../api'
import { formatMarkerTime, parseMarkerTime, type MarkerDraft, type VideoMarker } from '../videoMarkers'

interface Props {
  videoRef: RefObject<HTMLVideoElement | null>
  path: string
  initiallyPaused: boolean
  onEnded: () => void
  markers: VideoMarker[]
  markersAvailable: boolean
  canEditMarkers: boolean
  markerError: string | null
  onRetryMarkers: () => void
  onSaveMarker?: (draft: MarkerDraft, id?: number) => Promise<void>
  onDeleteMarker?: (id: number) => Promise<void>
}

function MarkerDialog({ initial, duration, onSave, onClose }: {
  initial: MarkerDraft & { id?: number }
  duration: number
  onSave: (draft: MarkerDraft, id?: number) => Promise<void>
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const [timestamp, setTimestamp] = useState(formatMarkerTime(initial.time_s))
  const [note, setNote] = useState(initial.note)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pending = useRef(false)
  useEffect(() => { ref.current?.showModal() }, [])
  const cancel = () => { if (!pending.current) onClose() }
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (pending.current) return
    const time = parseMarkerTime(timestamp)
    if (time === null || time > duration) {
      setError(`Enter a timestamp between 0:00 and ${formatMarkerTime(duration)}.`)
      return
    }
    pending.current = true
    setSaving(true); setError(null)
    try { await onSave({ time_s: time, note }, initial.id); onClose() }
    catch (e) { setError(String(e)) }
    finally { pending.current = false; setSaving(false) }
  }
  return <dialog ref={ref} className="marker-dialog" aria-labelledby="marker-dialog-title"
    onCancel={(e) => { e.preventDefault(); cancel() }} onKeyDown={(e) => e.stopPropagation()}
    onPointerDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
    <form onSubmit={(e) => void submit(e)}>
      <h2 id="marker-dialog-title">{initial.id === undefined ? 'Add marker' : 'Edit marker'}</h2>
      <label>Timestamp<input autoFocus value={timestamp} onChange={(e) => setTimestamp(e.target.value)} disabled={saving} aria-describedby="marker-time-help" /></label>
      <small id="marker-time-help">Seconds, MM:SS, or HH:MM:SS. Fractional seconds are supported.</small>
      <label>Note (optional)<textarea rows={4} maxLength={10000} value={note} onChange={(e) => setNote(e.target.value)} disabled={saving} /></label>
      {error && <p role="alert">{error}</p>}
      <div className="marker-dialog-actions"><button type="button" onClick={cancel} disabled={saving}>Cancel</button><button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button></div>
    </form>
  </dialog>
}

export default function VideoPlayer({ videoRef, path, initiallyPaused, onEnded, markers, markersAvailable, canEditMarkers,
  markerError, onRetryMarkers, onSaveMarker, onDeleteMarker }: Props) {
  const root = useRef<HTMLDivElement>(null)
  const scrubResume = useRef<boolean | null>(null)
  const editorResume = useRef(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [paused, setPaused] = useState(true)
  const [volume, setVolume] = useState(1)
  const [muted, setMuted] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [fullscreen, setFullscreen] = useState(false)
  const [controlsHidden, setControlsHidden] = useState(false)
  const [showMarkers, setShowMarkers] = useState(false)
  const [editor, setEditor] = useState<(MarkerDraft & { id?: number }) | null>(null)
  const [deleteId, setDeleteId] = useState<number | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const deletePending = useRef(false)

  useEffect(() => {
    const changed = () => {
      setFullscreen(document.fullscreenElement === root.current)
      setControlsHidden(false)
    }
    document.addEventListener('fullscreenchange', changed)
    return () => document.removeEventListener('fullscreenchange', changed)
  }, [])

  useEffect(() => {
    const player = root.current
    if (!fullscreen || !player) return
    const controls = player.querySelector('.video-controls')
    let timer: ReturnType<typeof setTimeout>
    let hoveringControls = false
    const scheduleHide = () => {
      clearTimeout(timer)
      timer = setTimeout(() => {
        // Keep controls available while using them, including keyboard focus,
        // an active seek gesture, and marker editing or delete confirmation.
        if (hoveringControls || controls?.querySelector(':focus-visible') ||
            scrubResume.current !== null || editor || deleteId !== null) {
          scheduleHide()
        } else setControlsHidden(true)
      }, 3000)
    }
    const reveal = () => { setControlsHidden(false); scheduleHide() }
    const pointer = (event: PointerEvent) => {
      hoveringControls = event.pointerType === 'mouse' && controls?.contains(event.target as Node) === true
      reveal()
    }
    const leave = () => { hoveringControls = false; scheduleHide() }
    player.addEventListener('pointermove', pointer)
    player.addEventListener('pointerdown', pointer)
    player.addEventListener('pointerleave', leave)
    player.addEventListener('keydown', reveal)
    player.addEventListener('focusin', reveal)
    player.addEventListener('focusout', scheduleHide)
    scheduleHide()
    return () => {
      clearTimeout(timer)
      player.removeEventListener('pointermove', pointer)
      player.removeEventListener('pointerdown', pointer)
      player.removeEventListener('pointerleave', leave)
      player.removeEventListener('keydown', reveal)
      player.removeEventListener('focusin', reveal)
      player.removeEventListener('focusout', scheduleHide)
    }
  }, [fullscreen, editor, deleteId])

  const play = () => {
    const video = videoRef.current
    if (video) void video.play().catch((e: unknown) => {
      // A subsequent pause/seek/navigation can cancel play before its promise
      // settles. That is a user action, not a playback failure.
      if (video.isConnected && !(e instanceof DOMException && e.name === 'AbortError')) {
        setError('Playback could not start. Press Play to retry.')
      }
    })
  }
  const togglePlay = () => {
    const video = videoRef.current
    if (!video) return
    setError(null)
    if (video.paused) play(); else video.pause()
  }
  const seek = (target: number) => {
    const video = videoRef.current
    if (!video || !duration) return
    video.currentTime = Math.max(0, Math.min(duration, target))
    setTime(video.currentTime)
  }
  const startScrub = () => {
    const video = videoRef.current
    if (!video || scrubResume.current !== null) return
    scrubResume.current = !video.paused && !video.ended
    video.pause()
  }
  const endScrub = () => {
    const resume = scrubResume.current
    scrubResume.current = null
    if (resume) play()
  }
  const beginEditor = (marker?: VideoMarker) => {
    const video = videoRef.current
    if (!video) return
    editorResume.current = !video.paused && !video.ended
    video.pause()
    setEditor(marker ? { id: marker.id, time_s: marker.time_s, note: marker.note } : { time_s: video.currentTime, note: '' })
  }
  const closeEditor = () => {
    setEditor(null)
    if (editorResume.current && videoRef.current?.isConnected) play()
    editorResume.current = false
  }
  const remove = async (id: number) => {
    if (!onDeleteMarker || deletePending.current) return
    deletePending.current = true
    setDeleting(true); setError(null)
    try { await onDeleteMarker(id); setDeleteId(null) }
    catch (e) { setError(String(e)) }
    finally { deletePending.current = false; setDeleting(false) }
  }
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement === root.current) await document.exitFullscreen()
      else await root.current?.requestFullscreen()
    } catch { setError('Fullscreen is unavailable in this browser.') }
  }

  return <div className={`video-player${controlsHidden ? ' video-player--controls-hidden' : ''}`} ref={root} onPointerDown={(e) => e.stopPropagation()}
    onKeyDown={(e) => {
      // Focused form controls own their keys; no player key reaches lightbox paging.
      e.stopPropagation()
      if (editor || (e.target as HTMLElement).closest('input,select,textarea,button,dialog')) return
      if (e.key === ' ' || e.key === 'k') { e.preventDefault(); togglePlay() }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); seek(time + (e.key === 'ArrowLeft' ? -5 : 5)) }
      if (e.key === 'Escape' && fullscreen) void document.exitFullscreen()
    }}>
    <video ref={videoRef} src={videoUrl(path)} poster={posterUrl(path)} autoPlay={!initiallyPaused} playsInline
      tabIndex={0} aria-label="Video playback" onEnded={() => { setPaused(true); onEnded() }}
      onPlay={() => setPaused(false)} onPause={() => setPaused(true)}
      onTimeUpdate={() => setTime(videoRef.current?.currentTime ?? 0)}
      onSeeked={() => setTime(videoRef.current?.currentTime ?? 0)}
      onLoadedMetadata={() => { const value = videoRef.current?.duration ?? 0; setDuration(Number.isFinite(value) ? value : 0) }}
      onDurationChange={() => { const value = videoRef.current?.duration ?? 0; setDuration(Number.isFinite(value) ? value : 0) }}
      onVolumeChange={() => { setVolume(videoRef.current?.volume ?? 1); setMuted(videoRef.current?.muted ?? false) }}
      onRateChange={() => setSpeed(videoRef.current?.playbackRate ?? 1)}
      onError={() => setError('This video could not be loaded.')}
    />
    <div className="video-controls" aria-label="Video controls">
      <div className="video-timeline">
        <div className="video-marker-ticks">{duration > 0 && markers.map((marker) => <button key={marker.id}
          className="video-marker-tick" style={{ left: `${Math.min(100, marker.time_s / duration * 100)}%` }}
          aria-label={`Jump to ${formatMarkerTime(marker.time_s)}${marker.note ? `: ${marker.note}` : ''}`}
          title={`${formatMarkerTime(marker.time_s)}${marker.note ? ` — ${marker.note}` : ''}`}
          onClick={() => seek(marker.time_s)}>◆</button>)}</div>
        <input type="range" aria-label="Seek video" aria-valuetext={`${formatMarkerTime(time)} of ${formatMarkerTime(duration)}`}
          min={0} max={duration || 1} step="0.001" value={Math.min(time, duration)} disabled={!duration || !!editor}
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); startScrub() }}
          onPointerUp={endScrub} onPointerCancel={endScrub} onLostPointerCapture={endScrub} onBlur={endScrub}
          onKeyDown={(e) => {
            const target = e.key === 'Home' ? 0 : e.key === 'End' ? duration
              : ['ArrowLeft', 'ArrowDown'].includes(e.key) ? time - 5
                : ['ArrowRight', 'ArrowUp'].includes(e.key) ? time + 5
                  : e.key === 'PageDown' ? time - duration / 10 : e.key === 'PageUp' ? time + duration / 10 : null
            if (target !== null) { e.preventDefault(); startScrub(); seek(target) }
          }}
          onKeyUp={endScrub} onChange={(e) => seek(Number(e.target.value))} />
      </div>
      <div className="video-controls-row">
        <button onClick={togglePlay} aria-label={paused ? 'Play video' : 'Pause video'}>{paused ? '▶' : '❚❚'}</button>
        <time className="video-time">
          {/* Reserve the longest label for this video, including all millisecond digits. */}
          <span className="video-time-size" aria-hidden="true">{`${formatMarkerTime(duration).split('.')[0]}.000 / ${formatMarkerTime(duration).split('.')[0]}.000`}</span>
          <span>{formatMarkerTime(time)} / {formatMarkerTime(duration)}</span>
        </time>
        <button onClick={() => { if (videoRef.current) videoRef.current.muted = !muted }} aria-label={muted ? 'Unmute video' : 'Mute video'}>{muted ? 'Unmute' : 'Mute'}</button>
        <input className="video-volume" aria-label="Volume" type="range" min={0} max={1} step="0.05" value={muted ? 0 : volume}
          onChange={(e) => { if (videoRef.current) { videoRef.current.volume = Number(e.target.value); videoRef.current.muted = false } }} />
        <select aria-label="Playback speed" value={speed} onChange={(e) => { if (videoRef.current) videoRef.current.playbackRate = Number(e.target.value) }}>
          {[0.25, 0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
        </select>
        <button onClick={() => void toggleFullscreen()} aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen video'}>⛶</button>
        {markersAvailable && <>
          {canEditMarkers && onSaveMarker && <button onClick={() => beginEditor()} disabled={!duration}>＋ Add marker</button>}
          <button aria-expanded={showMarkers} onClick={() => setShowMarkers((v) => !v)}>Markers ({markers.length})</button>
        </>}
      </div>
      {markerError && <p role="alert">{markerError} <button onClick={onRetryMarkers}>Retry markers</button></p>}
      {error && <p role="alert">{error}</p>}
      {showMarkers && <div className="video-marker-list">
        {markers.length === 0 && <p>No markers yet.</p>}
        <ul className="marker-list">{markers.map((marker) => <li key={marker.id}>
          <button className="marker-jump" onClick={() => seek(marker.time_s)}><time>{formatMarkerTime(marker.time_s)}</time><span>{marker.note || 'Untitled marker'}</span></button>
          {canEditMarkers && onSaveMarker && <button aria-label={`Edit marker at ${formatMarkerTime(marker.time_s)}`} onClick={() => beginEditor(marker)}>Edit</button>}
          {canEditMarkers && onDeleteMarker && (deleteId === marker.id ? <div className="marker-delete-confirm">
            <span>Delete marker?</span><button disabled={deleting} onClick={() => void remove(marker.id)}>Confirm delete</button><button disabled={deleting} onClick={() => setDeleteId(null)}>Cancel</button>
          </div> : <button aria-label={`Delete marker at ${formatMarkerTime(marker.time_s)}`} onClick={() => setDeleteId(marker.id)}>Delete</button>)}
        </li>)}</ul>
      </div>}
    </div>
    {editor && onSaveMarker && <MarkerDialog initial={editor} duration={duration} onSave={onSaveMarker} onClose={closeEditor} />}
  </div>
}
