import { useEffect, useMemo, useRef, useState } from 'react'
import { useInboxList } from '../useInboxList'
import {
  buildTree,
  allIds,
  dirState,
  selectAllState,
  toggleDir,
  toggleGroup,
  type TreeDir,
  type TriState,
} from '../inboxTree'

interface Props {
  // null => full import (select-all); a list => import just those capture groups.
  // `total` is the selected capture count, for the import progress bar.
  onProcess: (primaries: string[] | null, total: number) => void
  onClose: () => void
  busy: boolean
}

// A checkbox that renders the 'some' tri-state as the native indeterminate dash.
function TriCheckbox({
  state,
  onChange,
}: {
  state: TriState
  onChange: (on: boolean) => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === 'some'
  }, [state])
  return (
    <input
      type="checkbox"
      ref={ref}
      checked={state === 'all'}
      onChange={(e) => onChange(e.target.checked)}
    />
  )
}

function DirNode({
  dir,
  selected,
  setSelected,
}: {
  dir: TreeDir
  selected: Set<string>
  setSelected: (s: Set<string>) => void
}) {
  return (
    <ul className="inbox-tree">
      {dir.subdirs.map((sub) => (
        <li key={sub.path} className="inbox-dir">
          <label>
            <TriCheckbox
              state={dirState(sub, selected)}
              onChange={(on) => setSelected(toggleDir(sub, selected, on))}
            />
            <span className="inbox-dir-name">{sub.name}/</span>
          </label>
          <DirNode dir={sub} selected={selected} setSelected={setSelected} />
        </li>
      ))}
      {dir.groups.map((g) => (
        <li key={g.id} className="inbox-group">
          <label>
            <input
              type="checkbox"
              checked={selected.has(g.id)}
              onChange={(e) => setSelected(toggleGroup(g.id, selected, e.target.checked))}
            />
            <span className="inbox-group-name">{g.name}</span>
            {g.capture_kind && <span className="inbox-kind">{g.capture_kind}</span>}
            {g.file_count > 1 && <span className="inbox-count">×{g.file_count}</span>}
          </label>
        </li>
      ))}
    </ul>
  )
}

export default function InboxPanel({ onProcess, onClose, busy }: Props) {
  const { groups, loading, error, load } = useInboxList()
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // Fetch the inbox listing once when the panel opens.
  useEffect(() => {
    load()
  }, [load])

  const tree = useMemo(() => buildTree(groups), [groups])

  // Default selection = everything (select-all is used by default); re-applied
  // whenever the listing changes.
  useEffect(() => {
    setSelected(new Set(allIds(tree)))
  }, [tree])

  const masterState = selectAllState(tree, selected)
  const selectedCount = selected.size

  function process() {
    onProcess(masterState === 'all' ? null : [...selected], selectedCount)
    onClose()
  }

  return (
    <div className="inbox-panel">
      <div className="panel-head">
        <strong>Import from inbox</strong>
        <button onClick={onClose} aria-label="Close">×</button>
      </div>

      {loading && (
        <p className="inbox-note inbox-loading">
          <span className="spinner" aria-hidden="true" /> Scanning inbox…
        </p>
      )}
      {error && <p className="inbox-note inbox-error">{error}</p>}
      {!loading && !error && groups.length === 0 && (
        <p className="inbox-note">Inbox is empty.</p>
      )}

      {groups.length > 0 && (
        <>
          <label className="inbox-selectall">
            <TriCheckbox
              state={masterState}
              onChange={(on) => setSelected(on ? new Set(allIds(tree)) : new Set())}
            />
            <span>Select all ({groups.length} capture{groups.length === 1 ? '' : 's'})</span>
          </label>
          <div className="inbox-tree-scroll">
            <DirNode dir={tree} selected={selected} setSelected={setSelected} />
          </div>
        </>
      )}

      <div className="inbox-actions">
        <button onClick={process} disabled={busy || selectedCount === 0}>
          {busy
            ? 'Processing…'
            : `Process ${selectedCount} capture${selectedCount === 1 ? '' : 's'}`}
        </button>
      </div>
    </div>
  )
}
