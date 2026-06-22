// Pure date-grouping for the FileListPanel. Buckets the viewport's captures by day /
// month / year for the headings the panel renders above each group, and flattens the
// groups into a row model the @tanstack/react-virtual virtualizer can window over.
//
// Like captionInfo.ts, dates are parsed BY REGEX off the ISO string — never via
// `new Date(...)`, which would re-interpret the capture's local wall-clock instant in
// the VIEWER's browser timezone and could shift a capture across a day/month boundary.
// We read the literal Y/M/D fields so a capture is always grouped by the date it was shot.

import type { LibraryFeature, FeatureProps } from './types'

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

export type Granularity = 'day' | 'month' | 'year'

export interface DateParts {
  year: number
  month: number // 1..12
  day: number // 1..31
}

export interface DateGroup {
  key: string // sortable bucket key (zero-padded), '' for the Unknown-date group
  label: string // heading text, e.g. 'April 2024'
  files: LibraryFeature[]
}

export type RowItem =
  | { kind: 'header'; key: string; label: string }
  | { kind: 'thumbs'; key: string; files: LibraryFeature[] }

// Anchored extraction of the leading 'YYYY-MM-DD' from an ISO string (ignores any
// trailing time/offset). Returns null when the string doesn't start with a date.
function partsFrom(s: string | null | undefined): DateParts | null {
  if (!s) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s)
  if (!m) return null
  const month = Number(m[2])
  const day = Number(m[3])
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  return { year: Number(m[1]), month, day }
}

// Capture date: prefer the full local timestamp, fall back to the date-only local_date.
export function parseParts(props: Pick<FeatureProps, 'capture_ts_local' | 'local_date'>): DateParts | null {
  return partsFrom(props.capture_ts_local) ?? partsFrom(props.local_date)
}

function bucketKey(p: DateParts, gran: Granularity): string {
  const y = String(p.year).padStart(4, '0')
  const mo = String(p.month).padStart(2, '0')
  const d = String(p.day).padStart(2, '0')
  if (gran === 'year') return y
  if (gran === 'month') return `${y}-${mo}`
  return `${y}-${mo}-${d}`
}

function bucketLabel(p: DateParts, gran: Granularity): string {
  const month = MONTHS[p.month - 1]
  if (gran === 'year') return String(p.year)
  if (gran === 'month') return `${month} ${p.year}`
  return `${month} ${p.day}, ${p.year}`
}

// Group the files into dated buckets at the given granularity, ordered newest-first.
// Undated captures (parseParts === null) collect into a single trailing 'Unknown date'
// group. Files keep their incoming order within a group.
export function groupFeatures(files: LibraryFeature[], gran: Granularity): DateGroup[] {
  const dated = new Map<string, DateGroup>()
  const undated: LibraryFeature[] = []

  for (const f of files) {
    const parts = parseParts(f.properties)
    if (!parts) {
      undated.push(f)
      continue
    }
    const key = bucketKey(parts, gran)
    const existing = dated.get(key)
    if (existing) existing.files.push(f)
    else dated.set(key, { key, label: bucketLabel(parts, gran), files: [f] })
  }

  // Zero-padded keys sort lexicographically, so descending key === newest-first.
  const groups = [...dated.values()].sort((a, b) => (a.key < b.key ? 1 : a.key > b.key ? -1 : 0))
  if (undated.length > 0) groups.push({ key: '', label: 'Unknown date', files: undated })
  return groups
}

// Flatten groups into a virtualizable row list: each group emits one header row then
// ceil(files / columns) thumb rows. A thumb row never spans two groups, so headers
// always sit directly above their own thumbnails.
export function buildRowModel(groups: DateGroup[], columns: number): RowItem[] {
  const cols = Math.max(1, Math.floor(columns) || 1)
  const rows: RowItem[] = []
  for (const g of groups) {
    rows.push({ kind: 'header', key: `h:${g.key}`, label: g.label })
    for (let i = 0; i < g.files.length; i += cols) {
      rows.push({ kind: 'thumbs', key: `t:${g.key}:${i}`, files: g.files.slice(i, i + cols) })
    }
  }
  return rows
}
