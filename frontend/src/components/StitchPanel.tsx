import { listThumb } from '../api'
import { captionInfo } from '../captionInfo'
import LoadingImage from './LoadingImage'
import type { StitchState } from '../stitchJob'
import type { StitchAllProgress, StitchAllSummary } from '../stitchAllJob'
import type { LibraryFeature } from '../types'

interface Props {
  // Every panorama in the library whose stitch hasn't succeeded (capture_kind
  // panorama, has tiles, stitch_status !== 'ok'). Library-wide, NOT viewport-filtered.
  panoramas: LibraryFeature[]
  // Live per-file stitch state (App-level useStitch). Drives the status line + busy gate.
  stitchByFile: Record<number, StitchState>
  // Admin-only: start a stitch for one panorama. Undefined for a view-only viewer
  // (the list is still visible, but without the Stitch button).
  onStartStitch?: (fileId: number) => void
  // Admin-only: stitch every panorama in this list (the relocated "Stitch all"
  // action). Undefined for a view-only viewer — the bar then doesn't render. The run
  // state below is owned by App so it survives this panel unmounting on close.
  onStitchAll?: () => void
  onCancelStitchAll: () => void
  stitchAllRunning: boolean
  stitchAllProgress: StitchAllProgress | null
  stitchAllResult: StitchAllSummary | null
  // Open this panorama in the lightbox (e.g. to inspect the tiles / collage hero).
  onView: (feature: LibraryFeature) => void
  onClose: () => void
}

// Short status text for one panorama from its live stitch state, falling back to the
// stored stitch_status carried in the GeoJSON.
function statusText(f: LibraryFeature, st: StitchState | undefined): string {
  if (st) {
    if (st.state === 'pending') return 'queued…'
    if (st.state === 'running') {
      return st.step && st.step_total
        ? `stitching… step ${st.step}/${st.step_total}${st.step_name ? `: ${st.step_name}` : ''}`
        : 'stitching…'
    }
    if (st.state === 'error' || st.status === 'failed') return 'failed'
    if (st.status === 'unavailable') return 'Hugin unavailable'
    if (st.status === 'ok') return 'stitched ✓'
  }
  if (f.properties.stitch_status === 'pending') return 'stitching…'
  if (f.properties.stitch_status === 'failed') return 'last attempt failed'
  return 'not stitched'
}

// A live stitch is in flight for this file (pending/running) — disable its Stitch button.
function isBusy(st: StitchState | undefined): boolean {
  return st?.state === 'pending' || st?.state === 'running'
}

// Library-wide list of panoramas waiting to be stitched, so the user can see EXACTLY
// which sets are pending (not just the toolbar's count). Mirrors LocationPanel /
// QuarantinePanel as a toolbar-opened panel.
export default function StitchPanel({
  panoramas,
  stitchByFile,
  onStartStitch,
  onStitchAll,
  onCancelStitchAll,
  stitchAllRunning,
  stitchAllProgress,
  stitchAllResult,
  onView,
  onClose,
}: Props) {
  return (
    <div className="stitch-panel">
      <div className="panel-head">
        <strong>Unstitched panoramas ({panoramas.length})</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {onStitchAll && panoramas.length > 0 && (
        <div className="stitch-all-bar">
          {stitchAllRunning ? (
            <>
              <button onClick={onCancelStitchAll}>Cancel stitch-all</button>
              <span className="stitch-all-progress">
                {stitchAllProgress
                  ? `stitching ${stitchAllProgress.done}/${stitchAllProgress.total}`
                  : 'stitching…'}
              </span>
            </>
          ) : (
            <button onClick={onStitchAll}>
              Stitch all panoramas ({panoramas.length})
            </button>
          )}
          {!stitchAllRunning && stitchAllResult && stitchAllResult.failed > 0 && (
            <span className="stitch-all-failed">
              ⚠ {stitchAllResult.failed} stitch(es) failed
            </span>
          )}
        </div>
      )}

      {panoramas.length === 0 ? (
        <p className="inbox-note">No panoramas waiting to be stitched.</p>
      ) : (
        <ul className="stitch-list">
          {panoramas.map((f) => {
            const st = stitchByFile[f.properties.id]
            return (
              <li key={f.properties.id} className="stitch-item">
                <button
                  type="button"
                  className="stitch-thumb-btn"
                  onClick={() => onView(f)}
                  title="View this panorama"
                >
                  <LoadingImage
                    className="stitch-thumb"
                    src={listThumb(f.properties.media_type, f.properties.path)}
                    alt={f.properties.filename}
                  />
                </button>
                <div className="stitch-meta">
                  <span className="stitch-name">{f.properties.filename}</span>
                  <span className="stitch-sub">{captionInfo(f.properties)}</span>
                  <span className="stitch-status">{statusText(f, st)}</span>
                </div>
                {onStartStitch && (
                  <button
                    className="stitch-go"
                    onClick={() => onStartStitch(f.properties.id)}
                    // Also disabled during a stitch-all run: the batch drives stitches
                    // through runStitchAll (not the per-file useStitch hook), so `st`
                    // stays empty for batch items — without this gate the per-item
                    // button would invite a duplicate stitch for a file already queued.
                    disabled={isBusy(st) || stitchAllRunning}
                    title="Stitch this panorama"
                  >
                    {isBusy(st) ? '…' : 'Stitch'}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
