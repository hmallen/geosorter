// Pure trip-clustering helpers for the Trips panel: derive "trips" — runs of
// capture days at most `maxGapDays` apart — from the loaded library features,
// entirely client-side (no API call). Dates are 'YYYY-MM-DD' STRINGS throughout,
// never `new Date()` (see dateRange.ts); the only arithmetic is integer
// civil-date math, so clustering is timezone-stable in any browser.

import type { BBox } from './clusters'
import type { LibraryFeature } from './types'
import { featureDate } from './dateRange'
import { MONTHS } from './dateGroups'

export interface Trip {
  key: string // `${from}_${to}` — stable id for React keys
  from: string // 'YYYY-MM-DD', inclusive
  to: string // 'YYYY-MM-DD', inclusive
  count: number // captures in the trip
  bbox: BBox // covering [minLon, minLat, maxLon, maxLat]
  placeLabel: string // dominant place, '+N more places', or 'Unknown place'
  dateLabel: string // day-resolution range, e.g. 'Mar 9–17, 2024'
}

// Idle-day thresholds offered by the panel's gap select.
export const GAP_CHOICES = [1, 2, 3, 7] as const
export const DEFAULT_GAP_DAYS = 2

// Days since 1970-01-01 from a 'YYYY-MM-DD' string, by the days-from-civil
// algorithm (Hinnant) — pure integer math, no Date object involved.
export function dayOrdinal(date: string): number {
  let y = Number(date.slice(0, 4))
  const m = Number(date.slice(5, 7))
  const d = Number(date.slice(8, 10))
  if (m <= 2) y -= 1
  const era = Math.floor(y / 400)
  const yoe = y - era * 400
  const doy = Math.floor((153 * (m > 2 ? m - 3 : m + 9) + 2) / 5) + d - 1
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
}

function monthName(date: string): string {
  const m = Number(date.slice(5, 7))
  return MONTHS[m - 1]?.slice(0, 3) ?? date.slice(5, 7)
}

// Day-resolution range label: 'Mar 9, 2024' / 'Mar 9–17, 2024' /
// 'Mar 28 – Apr 2, 2024' / 'Dec 28, 2024 – Jan 3, 2025'. A same-month span
// keeps a tight en dash between the day numbers; anything wider spells both
// endpoints with a spaced en dash.
export function formatTripDates(from: string, to: string): string {
  const [fy, fm, fd] = [from.slice(0, 4), monthName(from), Number(from.slice(8, 10))]
  const [ty, tm, td] = [to.slice(0, 4), monthName(to), Number(to.slice(8, 10))]
  if (from === to) return `${fm} ${fd}, ${Number(fy)}`
  if (fy === ty && fm === tm) return `${fm} ${fd}–${td}, ${Number(fy)}`
  if (fy === ty) return `${fm} ${fd} – ${tm} ${td}, ${Number(fy)}`
  return `${fm} ${fd}, ${Number(fy)} – ${tm} ${td}, ${Number(ty)}`
}

interface TripAccum {
  from: string
  to: string
  count: number
  bbox: BBox
  placeCounts: Map<string, number>
}

function finalize(t: TripAccum): Trip {
  let placeLabel = 'Unknown place'
  if (t.placeCounts.size > 0) {
    let dominant = ''
    let best = -1
    for (const [place, n] of t.placeCounts) {
      if (n > best) {
        dominant = place
        best = n
      }
    }
    const extra = t.placeCounts.size - 1
    placeLabel =
      extra > 0 ? `${dominant} +${extra} more place${extra === 1 ? '' : 's'}` : dominant
  }
  return {
    key: `${t.from}_${t.to}`,
    from: t.from,
    to: t.to,
    count: t.count,
    bbox: t.bbox,
    placeLabel,
    dateLabel: formatTripDates(t.from, t.to),
  }
}

// Cluster the dated captures into trips: sort by capture date and start a new
// trip whenever the gap to the previous capture exceeds maxGapDays (a gap
// exactly equal to it stays in the same trip). Dateless captures are excluded —
// they cannot be placed on a timeline. Trips return newest-first.
export function buildTrips(features: LibraryFeature[], maxGapDays: number): Trip[] {
  const dated: { f: LibraryFeature; d: string }[] = []
  for (const f of features) {
    const d = featureDate(f.properties)
    if (d !== null) dated.push({ f, d })
  }
  dated.sort((a, b) => (a.d < b.d ? -1 : a.d > b.d ? 1 : 0))

  const trips: Trip[] = []
  let cur: TripAccum | null = null
  for (const { f, d } of dated) {
    const [lon, lat] = f.geometry.coordinates
    if (cur !== null && dayOrdinal(d) - dayOrdinal(cur.to) > maxGapDays) {
      trips.push(finalize(cur))
      cur = null
    }
    if (cur === null) {
      cur = { from: d, to: d, count: 0, bbox: [lon, lat, lon, lat], placeCounts: new Map() }
    }
    cur.to = d // sorted ascending, so d only ever extends the trip
    cur.count += 1
    cur.bbox = [
      Math.min(cur.bbox[0], lon),
      Math.min(cur.bbox[1], lat),
      Math.max(cur.bbox[2], lon),
      Math.max(cur.bbox[3], lat),
    ]
    const place = f.properties.place_string
    if (place) cur.placeCounts.set(place, (cur.placeCounts.get(place) ?? 0) + 1)
  }
  if (cur !== null) trips.push(finalize(cur))
  return trips.reverse()
}

// Case-insensitive substring filter over the place and date labels; a blank
// query keeps all (identity, mirroring filterPlaces). The date labels always
// carry day numbers ('Mar 9–17, 2024'), so a natural month-year query like
// 'Mar 2024' would never match them — each endpoint's '<Mon> <year>' token is
// searched as well.
export function filterTrips(trips: Trip[], query: string): Trip[] {
  const q = query.trim().toLowerCase()
  if (!q) return trips
  const monthYear = (d: string) => `${monthName(d)} ${Number(d.slice(0, 4))}`
  return trips.filter((t) =>
    `${t.placeLabel} ${t.dateLabel} ${monthYear(t.from)} ${monthYear(t.to)}`
      .toLowerCase()
      .includes(q),
  )
}
