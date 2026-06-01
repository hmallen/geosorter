import { useOrganizeJob } from '../useOrganizeJob'

export default function Toolbar({ onDone }: { onDone: () => void }) {
  const { job, running, start } = useOrganizeJob(onDone)

  return (
    <div className="toolbar">
      <button onClick={start} disabled={running}>
        {running ? 'Processing…' : 'Process Inbox'}
      </button>
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
