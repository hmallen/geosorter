import { listThumb } from '../api'
import { captionInfo } from '../captionInfo'
import LoadingImage from './LoadingImage'
import type { StitchState } from '../stitchJob'
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
  onView,
  onClose,
}: Props) {
  return (
    <div className="stitch-panel">
      <div className="panel-head">
        <strong>Unstitched panoramas ({panoramas.length})</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

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
                    disabled={isBusy(st)}
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
