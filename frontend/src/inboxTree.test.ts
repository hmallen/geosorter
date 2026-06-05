import { describe, it, expect } from 'vitest'
import {
  buildTree,
  allIds,
  dirGroupIds,
  dirState,
  toggleDir,
  toggleGroup,
  selectAllState,
  type InboxGroup,
  type TreeDir,
} from './inboxTree'

const groups: InboxGroup[] = [
  { id: 'top.JPG', dir: '', name: 'top.JPG', capture_kind: null, file_count: 1 },
  {
    id: 'DCIM/100MEDIA/DJI_0001.MP4', dir: 'DCIM/100MEDIA',
    name: 'DJI_0001.MP4', capture_kind: null, file_count: 2,
  },
  {
    id: 'DCIM/100MEDIA/DJI_0002.JPG', dir: 'DCIM/100MEDIA',
    name: 'DJI_0002.JPG', capture_kind: null, file_count: 1,
  },
  {
    id: 'DCIM/HYPERLAPSE/h.MP4', dir: 'DCIM/HYPERLAPSE',
    name: 'h.MP4', capture_kind: 'hyperlapse', file_count: 6,
  },
]

// Navigate to a nested dir node by its path segments.
function dirAt(root: TreeDir, ...segs: string[]): TreeDir {
  let node = root
  for (const seg of segs) {
    const next = node.subdirs.find((d) => d.name === seg)
    if (!next) throw new Error(`no subdir ${seg} under ${node.path}`)
    node = next
  }
  return node
}

describe('buildTree', () => {
  it('nests groups under their directory path, creating intermediate dirs', () => {
    const root = buildTree(groups)
    expect(root.groups.map((g) => g.id)).toEqual(['top.JPG']) // root-level capture
    expect(root.subdirs.map((d) => d.name)).toEqual(['DCIM'])
    const dcim = dirAt(root, 'DCIM')
    expect(dcim.subdirs.map((d) => d.name)).toEqual(['100MEDIA', 'HYPERLAPSE'])
    expect(dcim.groups).toEqual([]) // DCIM has no direct captures, only subdirs
    const media = dirAt(root, 'DCIM', '100MEDIA')
    expect(media.path).toBe('DCIM/100MEDIA')
    expect(media.groups.map((g) => g.id)).toEqual([
      'DCIM/100MEDIA/DJI_0001.MP4',
      'DCIM/100MEDIA/DJI_0002.JPG',
    ])
  })
})

describe('id collection', () => {
  it('allIds returns every leaf id under the root', () => {
    expect(new Set(allIds(buildTree(groups)))).toEqual(
      new Set([
        'top.JPG',
        'DCIM/100MEDIA/DJI_0001.MP4',
        'DCIM/100MEDIA/DJI_0002.JPG',
        'DCIM/HYPERLAPSE/h.MP4',
      ]),
    )
  })

  it('dirGroupIds returns leaves recursively under a dir', () => {
    const root = buildTree(groups)
    expect(new Set(dirGroupIds(dirAt(root, 'DCIM')))).toEqual(
      new Set([
        'DCIM/100MEDIA/DJI_0001.MP4',
        'DCIM/100MEDIA/DJI_0002.JPG',
        'DCIM/HYPERLAPSE/h.MP4',
      ]),
    )
  })
})

describe('dirState tri-state', () => {
  const root = buildTree(groups)
  const media = dirAt(root, 'DCIM', '100MEDIA')

  it('is all when every leaf under the dir is selected', () => {
    const sel = new Set(['DCIM/100MEDIA/DJI_0001.MP4', 'DCIM/100MEDIA/DJI_0002.JPG'])
    expect(dirState(media, sel)).toBe('all')
  })
  it('is some when only part is selected', () => {
    expect(dirState(media, new Set(['DCIM/100MEDIA/DJI_0001.MP4']))).toBe('some')
  })
  it('is none when nothing under the dir is selected', () => {
    expect(dirState(media, new Set(['top.JPG']))).toBe('none')
  })
})

describe('toggles return a new Set', () => {
  const root = buildTree(groups)

  it('toggleGroup adds and removes a single id', () => {
    const added = toggleGroup('top.JPG', new Set(), true)
    expect(added.has('top.JPG')).toBe(true)
    const removed = toggleGroup('top.JPG', added, false)
    expect(removed.has('top.JPG')).toBe(false)
    expect(added.has('top.JPG')).toBe(true) // original untouched (immutability)
  })

  it('toggleDir off removes every leaf under the dir, keeping siblings', () => {
    const all = new Set(allIds(root))
    const off = toggleDir(dirAt(root, 'DCIM'), all, false)
    expect(off.has('top.JPG')).toBe(true) // sibling kept
    expect(off.has('DCIM/100MEDIA/DJI_0001.MP4')).toBe(false)
    expect(off.has('DCIM/HYPERLAPSE/h.MP4')).toBe(false)
  })

  it('toggleDir on adds every leaf under the dir', () => {
    const on = toggleDir(dirAt(root, 'DCIM'), new Set(), true)
    expect(new Set(on)).toEqual(new Set(dirGroupIds(dirAt(root, 'DCIM'))))
  })
})

describe('selectAllState', () => {
  const root = buildTree(groups)
  it('all / some / none over the whole tree', () => {
    expect(selectAllState(root, new Set(allIds(root)))).toBe('all')
    expect(selectAllState(root, new Set(['top.JPG']))).toBe('some')
    expect(selectAllState(root, new Set())).toBe('none')
  })
})
