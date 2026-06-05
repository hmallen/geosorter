import { useState } from 'react'
import { useOrganizeJob } from '../useOrganizeJob'
import { useUndoJob } from '../useUndoJob'
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
  const busy = running || undoing

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
    </div>
  )
}
