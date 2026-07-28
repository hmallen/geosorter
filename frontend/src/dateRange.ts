// Pure month-resolution date-range model for the timeline scrubber (feature 3).
// Like dateGroups.ts / captionInfo.ts, dates are handled as 'YYYY-MM-DD' STRINGS —
// never via `new Date(...)`, which would re-interpret a capture's local wall-clock
// date in the viewer's browser timezone. 'YYYY-MM' / 'YYYY-MM-DD' strings compare
// correctly lexicographically, so all range logic is plain string comparison; the
// only arithmetic is the dense-month increment (simple year/month string math).

import { MONTHS, partsFrom, type DateParts } from './dateGroups'
import type { FeatureProps, LibraryFeature } from './types'

export interface MonthBin {
  key: string // 'YYYY-MM'
  count: number
}

export interface DateRange {
  from: string // 'YYYY-MM-DD', inclusive
  to: string // 'YYYY-MM-DD', inclusive
}

// Scrubber selection as indices into the MonthBin array (both inclusive).
export interface MonthSelection {
  start: number
  end: number
}

// A feature's date for range purposes: local_date, falling back to the leading
// date of capture_ts_local. null when neither starts with a plausible date —
// such features are EXCLUDED while a range is active (they can't be placed).
// Parsing/validation is dateGroups.partsFrom; the source PRIORITY is deliberately
// the reverse of dateGroups.parseParts (local_date is the organizer's canonical
// filing date), so only the selection lives here.
export function featureDate(
  props: Pick<FeatureProps, 'local_date' | 'capture_ts_local'>,
): string | null {
  const p: DateParts | null = partsFrom(props.local_date) ?? partsFrom(props.capture_ts_local)
  if (!p) return null
  return `${String(p.year).padStart(4, '0')}-${String(p.month).padStart(2, '0')}-${String(
    p.day,
  ).padStart(2, '0')}`
}

// 'YYYY-MM' + one month, by string math (rolls the year over at December).
function nextMonth(key: string): string {
  const y = Number(key.slice(0, 4))
  const m = Number(key.slice(5, 7))
  return m >= 12
    ? `${String(y + 1).padStart(4, '0')}-01`
    : `${key.slice(0, 4)}-${String(m + 1).padStart(2, '0')}`
}

// Cap the dense month axis so one corrupt far-future/past date (year 0001/9999)
// can't explode the bin array; 300 years is far beyond any real library span.
const MAX_MONTHS = 3600

// Dense per-month histogram from the earliest to the latest dated capture,
// zero-count months included so the scrubber's axis is linear. [] when no
// feature carries a usable date.
export function buildTimeline(features: LibraryFeature[]): MonthBin[] {
  const counts = new Map<string, number>()
  let min: string | null = null
  let max: string | null = null
  for (const f of features) {
    const d = featureDate(f.properties)
    if (!d) continue
    const k = d.slice(0, 7)
    counts.set(k, (counts.get(k) ?? 0) + 1)
    if (min === null || k < min) min = k
    if (max === null || k > max) max = k
  }
  if (min === null || max === null) return []
  const bins: MonthBin[] = []
  for (let k = min; k <= max && bins.length < MAX_MONTHS; k = nextMonth(k)) {
    bins.push({ key: k, count: counts.get(k) ?? 0 })
  }
  return bins
}

// Inclusive filter on the feature's date. A null range keeps everything
// (identity — callers can pass the array through); with a range active,
// dateless features are excluded.
export function filterByDateRange(
  features: LibraryFeature[],
  range: DateRange | null,
): LibraryFeature[] {
  if (!range) return features
  return features.filter((f) => {
    const d = featureDate(f.properties)
    return d !== null && d >= range.from && d <= range.to
  })
}

function clampIdx(i: number, len: number): number {
  return Math.max(0, Math.min(len - 1, Math.floor(i)))
}

// Selection indices -> the inclusive date range covering those whole months.
// The '-31' upper bound is safe under string comparison (no real date exceeds
// it), so no per-month day count is needed. Indices are clamped and may arrive
// in either order. null only when there are no months at all.
export function rangeFromMonths(
  months: MonthBin[],
  startIdx: number,
  endIdx: number,
): DateRange | null {
  if (months.length === 0) return null
  const a = clampIdx(Math.min(startIdx, endIdx), months.length)
  const b = clampIdx(Math.max(startIdx, endIdx), months.length)
  return { from: `${months[a].key}-01`, to: `${months[b].key}-31` }
}

// The inverse mapping for the scrubber: which month indices does a stored range
// select? A null range (no filter) reads as the full span. A range that only
// partially overlaps the axis clamps to the covered months; one entirely outside
// it degenerates to the nearest edge month.
export function selectionFromRange(
  months: MonthBin[],
  range: DateRange | null,
): MonthSelection {
  const last = months.length - 1
  if (last < 0) return { start: 0, end: 0 }
  if (!range) return { start: 0, end: last }
  const fromKey = range.from.slice(0, 7)
  const toKey = range.to.slice(0, 7)
  let start = months.findIndex((m) => m.key >= fromKey)
  if (start === -1) start = last // whole range after the axis
  let end = 0
  for (let i = last; i >= 0; i--) {
    if (months[i].key <= toKey) {
      end = i
      break
    }
  }
  if (end < start) end = start // disjoint/inverted after clamping
  return { start, end }
}

// 'Jan 2025 – Jun 2025' (or just 'Jan 2025' for a single-month range). Short
// month names derive from dateGroups' MONTHS table — no Date involved.
export function formatRangeLabel(range: DateRange): string {
  const label = (d: string) => {
    const m = Number(d.slice(5, 7))
    const name = MONTHS[m - 1]?.slice(0, 3) ?? d.slice(5, 7)
    return `${name} ${Number(d.slice(0, 4))}`
  }
  const a = label(range.from)
  const b = label(range.to)
  return a === b ? a : `${a} – ${b}`
}

// Pointer x-offset within the bar strip -> month index (clamped). Each month
// owns an equal 1/count share of the width.
export function indexFromPointer(offsetX: number, width: number, count: number): number {
  if (count <= 0) return 0
  if (width <= 0) return 0
  return clampIdx((offsetX / width) * count, count)
}

// Bar height as a percentage of the tallest month. Zero-count months render no
// bar (0); any non-zero month keeps a visible minimum so sparse months stay
// clickable landmarks.
export function barHeightPct(count: number, maxCount: number): number {
  if (count <= 0 || maxCount <= 0) return 0
  return Math.max(8, Math.round((count / maxCount) * 100))
}

// Handle x-positions as percentages of the strip width: the start handle sits
// on its month's left edge, the end handle on its month's right edge.
export function handleLeftPct(sel: MonthSelection, count: number): { start: number; end: number } {
  if (count <= 0) return { start: 0, end: 100 }
  return { start: (sel.start / count) * 100, end: ((sel.end + 1) / count) * 100 }
}
