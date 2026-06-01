import { useOrganizeJob } from '../useOrganizeJob'
import { useInboxCount } from '../useInboxCount'

export default function Toolbar({ onDone }: { onDone: () => void }) {
  const { count, refresh } = useInboxCount()
  // After an organize run, reload the library AND refresh the inbox badge (it should
  // drop to empty without waiting for the next poll tick).
  const afterRun = () => {
    onDone()
    refresh()
  }
  const { job, running, start } = useOrganizeJob(afterRun)
import { useUndoJob } from '../useUndoJob'

export default function Toolbar({ onDone }: { onDone: () => void }) {
  const { job, running, start } = useOrganizeJob(onDone)
  const { undo, undoing, startUndo } = useUndoJob(onDone)
  const busy = running || undoing

  return (
    <div className="toolbar">
      <button onClick={start} disabled={busy}>
        {running ? 'Processing…' : 'Process Inbox'}
      </button>
      <span className="inbox">
        {count.files > 0
          ? `inbox: ${count.captures} capture${count.captures === 1 ? '' : 's'} ` +
            `(${count.files} file${count.files === 1 ? '' : 's'})`
          : 'inbox empty'}
      </span>
      <button onClick={startUndo} disabled={busy}>
        {undoing ? 'Undoing…' : 'Undo Last Batch'}
      </button>
      {job && (
        <span className="job">
          {job.state === 'running'
            ? `processing ${job.processed}${job.current ? ` — ${job.current}` : ''}`
            : `${job.state}: organized ${job.organized}, quarantined ${job.quarantined}` +
              (job.failures.length ? `, errors ${job.failures.length}` : '')}
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
