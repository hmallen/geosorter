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

  return (
    <div className="toolbar">
      <button onClick={start} disabled={running}>
        {running ? 'Processing…' : 'Process Inbox'}
      </button>
      <span className="inbox">
        {count.files > 0
          ? `inbox: ${count.captures} capture${count.captures === 1 ? '' : 's'} ` +
            `(${count.files} file${count.files === 1 ? '' : 's'})`
          : 'inbox empty'}
      </span>
      {job && (
        <span className="job">
          {job.state === 'running'
            ? `processing ${job.processed}${job.current ? ` — ${job.current}` : ''}`
            : `${job.state}: organized ${job.organized}, quarantined ${job.quarantined}` +
              (job.failures.length ? `, errors ${job.failures.length}` : '')}
        </span>
      )}
    </div>
  )
}
