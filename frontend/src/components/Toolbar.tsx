import { useState } from 'react'
import { useOrganizeJob } from '../useOrganizeJob'
import { useUndoJob } from '../useUndoJob'
import { useRescanJob } from '../useRescanJob'
import { useInboxCount } from '../useInboxCount'
import { progressLabel, loadProgressLabel, resultLabel } from '../organizeJob'
import InboxPanel from './InboxPanel'

export default function Toolbar({ onDone }: { onDone: () => void }) {
  const { count, refresh } = useInboxCount()
  const [picking, setPicking] = useState(false)
  // After an organize OR undo run, reload the library AND refresh the inbox badge
  // (organize empties the inbox, undo refills it) without waiting for the next poll.
  const afterRun = () => {
    onDone()
    refresh()
  }
  const { job, running, total, start } = useOrganizeJob(afterRun)
  const { undo, undoing, startUndo } = useUndoJob(afterRun)
  const { rescan, rescanning, startRescan } = useRescanJob(afterRun)
  const busy = running || undoing || rescanning

  return (
    <div className="toolbar">
      <button onClick={() => setPicking(true)} disabled={busy}>
        {running ? 'Processing…' : 'Process Inbox'}
      </button>
      {picking && (
        <InboxPanel
          busy={busy}
          onClose={() => setPicking(false)}
          onProcess={(primaries, count) => start(primaries, count)}
        />
      )}
      <span className="inbox">
        {count.files > 0
          ? `inbox: ${count.captures} capture${count.captures === 1 ? '' : 's'} ` +
            `(${count.files} file${count.files === 1 ? '' : 's'})`
          : 'inbox empty'}
      </span>
      <button onClick={startUndo} disabled={busy}>
        {undoing ? 'Undoing…' : 'Undo Last Batch'}
      </button>
      <button onClick={startRescan} disabled={busy}>
        {rescanning ? 'Rescanning…' : 'Rescan Library'}
      </button>
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
    </div>
  )
}
