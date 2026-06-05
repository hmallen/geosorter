// Pure directory-tree model + selection helpers for the inbox import-selection UI.
// A leaf is a DJI capture GROUP (primary + companions), keyed by its inbox-relative
// POSIX primary path (`id`). Selecting a directory selects every group under it, so a
// primary always travels with its companions and capture-group atomicity is preserved.
// Selection state lives in a plain `Set<string>` of selected ids; every mutator
// returns a NEW set (immutable) so React state updates stay predictable.

export interface InboxGroup {
  id: string
  dir: string // inbox-relative POSIX parent dir, '' for a root-level capture
  name: string
  capture_kind: string | null
  file_count: number
}

export interface TreeDir {
  path: string // full inbox-relative dir path, '' for the root
  name: string // last path segment ('' for the root)
  subdirs: TreeDir[]
  groups: InboxGroup[]
}

function emptyDir(path: string, name: string): TreeDir {
  return { path, name, subdirs: [], groups: [] }
}

// Build the nested directory tree from a flat group list, creating intermediate
// directory nodes as needed. Subdirs and groups are sorted (by name / id) so the
// rendered tree and the tests are deterministic.
export function buildTree(groups: InboxGroup[]): TreeDir {
  const root = emptyDir('', '')
  for (const g of groups) {
    let node = root
    if (g.dir !== '') {
      for (const seg of g.dir.split('/')) {
        const childPath = node.path === '' ? seg : `${node.path}/${seg}`
        let child = node.subdirs.find((d) => d.name === seg)
        if (!child) {
          child = emptyDir(childPath, seg)
          node.subdirs.push(child)
        }
        node = child
      }
    }
    node.groups.push(g)
  }
  sortDir(root)
  return root
}

function sortDir(dir: TreeDir): void {
  dir.subdirs.sort((a, b) => a.name.localeCompare(b.name))
  dir.groups.sort((a, b) => a.id.localeCompare(b.id))
  for (const sub of dir.subdirs) sortDir(sub)
}

// Every leaf id under a directory (recursive).
export function dirGroupIds(dir: TreeDir): string[] {
  const ids = dir.groups.map((g) => g.id)
  for (const sub of dir.subdirs) ids.push(...dirGroupIds(sub))
  return ids
}

// Every leaf id in the whole tree.
export function allIds(root: TreeDir): string[] {
  return dirGroupIds(root)
}

export type TriState = 'all' | 'some' | 'none'

function tristate(ids: string[], selected: Set<string>): TriState {
  if (ids.length === 0) return 'none'
  let on = 0
  for (const id of ids) if (selected.has(id)) on++
  if (on === 0) return 'none'
  if (on === ids.length) return 'all'
  return 'some'
}

// Tri-state of a directory: all / some / none of its leaves selected.
export function dirState(dir: TreeDir, selected: Set<string>): TriState {
  return tristate(dirGroupIds(dir), selected)
}

// Tri-state of the whole tree (drives the master Select-All checkbox).
export function selectAllState(root: TreeDir, selected: Set<string>): TriState {
  return tristate(allIds(root), selected)
}

// Add (`on`) or remove (`!on`) a single group id; returns a new Set.
export function toggleGroup(id: string, selected: Set<string>, on: boolean): Set<string> {
  const next = new Set(selected)
  if (on) next.add(id)
  else next.delete(id)
  return next
}

// Add (`on`) or remove (`!on`) every leaf under a directory; returns a new Set.
export function toggleDir(dir: TreeDir, selected: Set<string>, on: boolean): Set<string> {
  const next = new Set(selected)
  for (const id of dirGroupIds(dir)) {
    if (on) next.add(id)
    else next.delete(id)
  }
  return next
}
