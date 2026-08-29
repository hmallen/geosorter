import { useEffect, useRef, useState } from 'react'
import { useOrganizeJob } from '../useOrganizeJob'
import { useUndoJob } from '../useUndoJob'
import { useRescanJob } from '../useRescanJob'
import { useInboxCount } from '../useInboxCount'
import { useInboxList } from '../useInboxList'
import { progressLabel, loadProgressLabel, resultLabel } from '../organizeJob'
import InboxPanel from './InboxPanel'
import LoginControl from './LoginControl'
import {
  CopyIcon,
  HeartIcon,
  InboxIcon,
  PanoramaIcon,
  PinIcon,
  PinOffIcon,
  RescanIcon,
  RouteIcon,
  TimelineIcon,
  UndoIcon,
  WrenchIcon,
} from './icons'

interface ToolbarProps {
  // Whether the viewer is an admin (m-implement-view-only-admin-auth). When false the
  // management actions are hidden and only the view-only controls (Locations, the
  // inbox badge) plus the Log-in control render.
  admin: boolean
  onDone: () => void
  // file_ids of panoramas that still want a stitch (capture_kind panorama, has
  // tiles, stitch_status !== 'ok'); labels the "Unstitched panoramas (N)" button.
  stitchTargets: number[]
  // Open the No-GPS panel (owned by App, which holds the assign hook + map
  // placement). `noGpsCount` labels the button so the user sees the backlog.
  onOpenNoGps: () => void
  noGpsCount: number
  // Open the location-filter panel (owned by App, which holds the place list +
  // map flyTo). Lets the user jump the map to any place in the library by name.
  onOpenLocations: () => void
  // Open the trips panel (owned by App): auto-derived trips over the library;
  // picking one date-filters the app and fits the camera. Public, like Locations.
  onOpenTrips: () => void
  // Open the unstitched-panorama panel (owned by App) listing exactly which panorama
  // sets are waiting to be stitched. Admin-only (stitching is an admin action).
  onOpenStitch: () => void
  // Open the duplicate-review panel (owned by App). `duplicatesCount` labels the
  // button; hidden for non-admins and when the backlog is empty (like No-GPS).
  onOpenDuplicates: () => void
  duplicatesCount: number
  // Open the broken-capture repair panel (owned by App). Admin-only: it scans
  // the quarantine for corrupt files and repairs/deletes them.
  onOpenRepair: () => void
  // Toggle the timeline scrubber (date-range brush over the map).
  onToggleTimeline: () => void
  timelineOn: boolean
  // Toggle the favorites-only view (App-level filter before map + list).
  onToggleFavorites: () => void
  favoritesOn: boolean
}

export default function Toolbar({
  admin,
  onDone,
  stitchTargets,
  onOpenNoGps,
  noGpsCount,
  onOpenLocations,
  onOpenTrips,
  onOpenStitch,
  onOpenDuplicates,
  duplicatesCount,
  onOpenRepair,
  onToggleTimeline,
  timelineOn,
  onToggleFavorites,
  favoritesOn,
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

  // The toolbar wraps to several rows on narrow windows, so overlays that sit
  // "under the toolbar" (.chips-row) cannot assume a one-row height. Publish
  // the live bottom edge as a CSS variable and let the CSS offset from it.
  // Measured via getBoundingClientRect (viewport coords == .app coords, which
  // is position:fixed inset:0) because the pill's offsetParent is now the
  // .toolbar-slot strip, so offsetTop no longer includes the strip's own top.
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = rootRef.current
    if (!el) return
    const publish = () =>
      document.documentElement.style.setProperty(
        '--toolbar-bottom',
        `${el.getBoundingClientRect().bottom}px`,
      )
    publish()
    const ro = new ResizeObserver(publish)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Full inbox detail lives in the tooltip; the button badge carries the capture
  // count so the old free-floating "inbox: N captures (M files)" text can go.
  const inboxTitle =
    count.files > 0
      ? `${count.captures} capture${count.captures === 1 ? '' : 's'} ` +
        `(${count.files} file${count.files === 1 ? '' : 's'}) waiting in the inbox`
      : 'Inbox is empty'
  // The review-backlog group only renders when at least one backlog exists.
  const hasBacklog = noGpsCount > 0 || duplicatesCount > 0 || stitchTargets.length > 0
  const hasStatus = Boolean(job || undo || rescan)

  return (
    // The slot is a transparent full-width strip that centers the pill over the
    // map area; the pill itself shrink-wraps and wraps its groups when narrow.
    <div className="toolbar-slot">
      <div className="toolbar" ref={rootRef}>
        <span className="brand" title="geosorter">
          {/* Pin mark reusing the favicon's cyan→green gradient (see favicon.svg). */}
          <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
            <defs>
              <linearGradient id="brand-pin" x1="6" y1="2" x2="18" y2="22" gradientUnits="userSpaceOnUse">
                <stop offset="0" stopColor="#67e8f9" />
                <stop offset="1" stopColor="#22c55e" />
              </linearGradient>
            </defs>
            <path
              d="M12 1.5a7.5 7.5 0 0 0-7.5 7.5c0 5.4 6.3 12.4 7 13.1a.7.7 0 0 0 1 0c.7-.7 7-7.7 7-13.1A7.5 7.5 0 0 0 12 1.5z"
              fill="url(#brand-pin)"
            />
            <circle cx="12" cy="9" r="3.1" fill="#0f172a" />
            <circle cx="12" cy="9" r="1.25" fill="#f8fafc" />
          </svg>
          geosorter
        </span>
        <span className="tb-sep" aria-hidden="true" />
        {admin && (
          <>
            <div className="tb-group">
              <button
                className="tb-primary"
                onClick={() => {
                  const next = !picking
                  setPicking(next)
                  if (next) loadInbox()
                }}
                disabled={busy}
                title={inboxTitle}
              >
                <InboxIcon className="tb-ico" />
                {running ? 'Processing…' : 'Process Inbox'}
                {count.captures > 0 && <span className="tb-count">{count.captures}</span>}
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
              <button onClick={startUndo} disabled={busy} title="Undo the last organize batch">
                <UndoIcon className="tb-ico" />
                {undoing ? 'Undoing…' : 'Undo'}
              </button>
              <button onClick={startRescan} disabled={busy} title="Rescan the library for on-disk changes">
                <RescanIcon className="tb-ico" />
                {rescanning ? 'Rescanning…' : 'Rescan'}
              </button>
              <button
                onClick={onOpenRepair}
                title="Scan for corrupt captures and repair or delete them"
              >
                <WrenchIcon className="tb-ico" />
                Repair
              </button>
            </div>
            <span className="tb-sep" aria-hidden="true" />
          </>
        )}
        <div className="tb-group">
          <button onClick={onOpenLocations} title="Jump the map to any place in the library">
            <PinIcon className="tb-ico" />
            Locations
          </button>
          <button onClick={onOpenTrips} title="Browse the library as auto-detected trips">
            <RouteIcon className="tb-ico" />
            Trips
          </button>
          <button
            className={timelineOn ? 'tb-toggle--on' : undefined}
            onClick={onToggleTimeline}
            aria-pressed={timelineOn}
            title="Show the date-range timeline over the map"
          >
            <TimelineIcon className="tb-ico" />
            Timeline
          </button>
          <button
            className={favoritesOn ? 'tb-toggle--on' : undefined}
            onClick={onToggleFavorites}
            aria-pressed={favoritesOn}
            title="Show only favorited captures"
          >
            <HeartIcon className="tb-ico" filled={favoritesOn} />
            Favorites
          </button>
        </div>
        {admin && hasBacklog && (
          <>
            <span className="tb-sep" aria-hidden="true" />
            <div className="tb-group">
              {noGpsCount > 0 && (
                <button onClick={onOpenNoGps} title="Place captures that arrived without GPS">
                  <PinOffIcon className="tb-ico" />
                  No GPS
                  <span className="tb-count">{noGpsCount}</span>
                </button>
              )}
              {duplicatesCount > 0 && (
                <button
                  onClick={onOpenDuplicates}
                  title="Review inbox captures skipped as duplicates of organized files"
                >
                  <CopyIcon className="tb-ico" />
                  Duplicates
                  <span className="tb-count">{duplicatesCount}</span>
                </button>
              )}
              {stitchTargets.length > 0 && (
                <button
                  onClick={onOpenStitch}
                  title="See exactly which panorama sets are waiting to be stitched"
                >
                  <PanoramaIcon className="tb-ico" />
                  Panoramas
                  <span className="tb-count">{stitchTargets.length}</span>
                </button>
              )}
            </div>
          </>
        )}
        {hasStatus && <span className="tb-sep" aria-hidden="true" />}
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
    </div>
  )
}
