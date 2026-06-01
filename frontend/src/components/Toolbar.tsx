import { useOrganizeJob } from '../useOrganizeJob'
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
