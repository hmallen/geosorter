import { useEffect, useRef, useState } from 'react'
import { useOrganizeJob } from '../useOrganizeJob'
import { useUndoJob } from '../useUndoJob'
import { useRescanJob } from '../useRescanJob'
import { useStitchAll } from '../useStitchAll'
import { useInboxCount } from '../useInboxCount'
import { useInboxList } from '../useInboxList'
import { progressLabel, loadProgressLabel, resultLabel } from '../organizeJob'
import InboxPanel from './InboxPanel'
import LoginControl from './LoginControl'

interface ToolbarProps {
  // Whether the viewer is an admin (m-implement-view-only-admin-auth). When false the
  // management actions are hidden and only the view-only controls (Locations, the
  // inbox badge) plus the Log-in control render.
  admin: boolean
  onDone: () => void
  // file_ids of panoramas that still want a stitch (capture_kind panorama, has
  // tiles, stitch_status !== 'ok'); the "Stitch all" button targets exactly these.
  stitchTargets: number[]
  // Reload just the library feed after a stitch-all completes (so finished
  // panoramas drop out of stitchTargets) without clearing the open selection.
  onReload: () => void
  // Open the No-GPS panel (owned by App, which holds the assign hook + map
  // placement). `noGpsCount` labels the button so the user sees the backlog.
  onOpenNoGps: () => void
  noGpsCount: number
  // Open the location-filter panel (owned by App, which holds the place list +
  // map flyTo). Lets the user jump the map to any place in the library by name.
  onOpenLocations: () => void
}

export default function Toolbar({
  admin,
  onDone,
  stitchTargets,
  onReload,
  onOpenNoGps,
  noGpsCount,
  onOpenLocations,
}: ToolbarProps) {
  // Suspend the inbox-badge poll while a destructive job runs (synced to `busy` in the
  // effect below). Stable ref -> the useInboxCount interval is established once, not
  // reset each render; the interval reads pausedRef.current freshly at each tick.
  // Start paused when not admin so a view-only viewer issues no /api/inbox fetch at
  // all (the inbox badge is admin-only — see the JSX gate below).
  const pausedRef = useRef(!admin)
  const { count, refresh } = useInboxCount(5000, pausedRef)
  // The inbox listing is owned here (Toolbar is alive from app startup) so the scan
  // runs once on mount and the Process Inbox panel opens pre-populated instead of
  // showing a "Scanning inbox…" delay each time.
  const { groups, loading: inboxLoading, error: inboxError, load: loadInbox } = useInboxList()
  const [picking, setPicking] = useState(false)

  useEffect(() => {
    if (admin) loadInbox()
  }, [admin, loadInbox])

  // After an organize OR undo run, reload the library AND refresh the inbox badge
  // (organize empties the inbox, undo refills it) without waiting for the next poll.
  // Also refresh the inbox listing so the panel reflects the new on-disk state.
  const afterRun = () => {
    onDone()
    refresh()
    loadInbox()
  }
  const { job, running, total, start } = useOrganizeJob(afterRun)
  const { undo, undoing, startUndo } = useUndoJob(afterRun)
  const { rescan, rescanning, startRescan } = useRescanJob(afterRun)
  // Stitch-all runs on the independent stitch pool, so it is NOT gated by `busy`
  // (organize/undo/rescan) — only by its own running flag.
  const stitchAll = useStitchAll(onReload)
  const busy = running || undoing || rescanning
  // Write the ref in an effect (not during render — react-hooks/refs) so the inbox poll
  // suspends while a destructive job runs OR while the viewer is not admin (the badge is
  // hidden for non-admins, so polling for it would be wasted /api/inbox traffic).
  // refresh() bypasses the pause gate, so entering admin (login / auth-off probe
  // resolving) populates the badge immediately instead of waiting a full poll tick.
  useEffect(() => {
    pausedRef.current = busy || !admin
    if (admin && !busy) refresh()
  }, [busy, admin, refresh])

  return (
    <div className="toolbar">
      {admin && (
        <>
          <button onClick={() => { setPicking(true); loadInbox() }} disabled={busy}>
            {running ? 'Processing…' : 'Process Inbox'}
          </button>
          {picking && (
            <InboxPanel
              busy={busy}
              groups={groups}
              loading={inboxLoading}
              error={inboxError}
              onClose={() => setPicking(false)}
              onProcess={(primaries, count) => start(primaries, count)}
            />
          )}
        </>
      )}
      {admin && (
        <span className="inbox">
          {count.files > 0
            ? `inbox: ${count.captures} capture${count.captures === 1 ? '' : 's'} ` +
              `(${count.files} file${count.files === 1 ? '' : 's'})`
            : 'inbox empty'}
        </span>
      )}
      {admin && (
        <>
          <button onClick={startUndo} disabled={busy}>
            {undoing ? 'Undoing…' : 'Undo Last Batch'}
          </button>
          <button onClick={startRescan} disabled={busy}>
            {rescanning ? 'Rescanning…' : 'Rescan Library'}
          </button>
        </>
      )}
      <button onClick={onOpenLocations}>Locations</button>
      {admin && noGpsCount > 0 && (
        <button onClick={onOpenNoGps}>No-GPS ({noGpsCount})</button>
      )}
      {admin &&
        (stitchAll.running ? (
          <span className="job job--progress">
            <button onClick={stitchAll.cancel}>Cancel stitch-all</button>
            {stitchAll.progress
              ? ` stitching ${stitchAll.progress.done}/${stitchAll.progress.total}`
              : ' stitching…'}
          </span>
        ) : (
          stitchTargets.length > 0 && (
            <button onClick={() => stitchAll.start(stitchTargets)}>
              Stitch all panoramas ({stitchTargets.length})
            </button>
          )
        ))}
      {admin && !stitchAll.running && stitchAll.result && stitchAll.result.failed > 0 && (
        <span className="job" title="See the ⚠ stitch badges in the file list">
          ⚠ {stitchAll.result.failed} stitch(es) failed
        </span>
      )}
      {job && job.state === 'running' && total !== null && (
        <span className="job job--progress" title={progressLabel(job)}>
          <progress value={Math.min(job.processed, total)} max={total} />
          {loadProgressLabel(job.processed, total)}
        </span>
      )}
      {job && job.state === 'running' && total === null && (
        <span className="job">{progressLabel(job)}</span>
      )}
      {job && job.state !== 'running' && (
        <span
          className={`job${job.state === 'error' ? ' job--error' : ''}`}
          title={job.error ?? undefined}
        >
          {resultLabel(job)}
        </span>
      )}
      {undo && (
        <span className="job">
          {undo.state === 'running'
            ? `undoing ${undo.processed}${undo.current ? ` — ${undo.current}` : ''}`
            : undo.nothing_to_undo
              ? 'nothing to undo'
              : `${undo.state}: restored ${undo.restored}` +
                (undo.conflicts.length ? `, conflicts ${undo.conflicts.length}` : '') +
                (undo.failures.length ? `, errors ${undo.failures.length}` : '')}
        </span>
      )}
      {rescan && (
        <span
          className={`job${rescan.state === 'error' ? ' job--error' : ''}`}
          title={rescan.error ?? undefined}
        >
          {rescan.state === 'running'
            ? `rescanning ${rescan.processed}${rescan.current ? ` — ${rescan.current}` : ''}`
            : `${rescan.state}: pruned ${rescan.pruned}` +
              (rescan.warnings.length ? `, warnings ${rescan.warnings.length}` : '') +
              (rescan.orphaned.length ? `, orphaned ${rescan.orphaned.length}` : '')}
        </span>
      )}
      <LoginControl />
    </div>
  )
}
