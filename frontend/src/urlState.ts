// Shareable URL state (feature 2): the app's restorable view lives in the URL
// hash, query-style —
// `#map=<zoom>/<lat>/<lon>&place=...&cap=...&from=...&to=...&bbox=...&fav=1`
// (`map` follows the OSM zoom/lat/lon convention). Parsed once on load, written
// with a debounced history.replaceState. Pure + Vitest-testable: parseHash is
// TOLERANT (a malformed field is dropped individually, unknown keys are ignored,
// nothing ever throws) and formatHash is roundtrip-stable with a fixed key order.

import { normalizeLon } from './viewport'
import type { BBox } from './clusters'

export interface UrlView {
  longitude: number
  latitude: number
  zoom: number
}

export interface UrlState {
  view?: UrlView
  place?: string // place_string breadcrumb from the Locations panel
  cap?: number // open capture's file id
  from?: string // active date-range filter, 'YYYY-MM-DD'
  to?: string
  bbox?: BBox // trip pick's geographic filter, [west, south, east, north]
  fav?: boolean // favorites-only filter active
}

// Anchored 'YYYY-MM-DD' with sane month/day fields (string-compared elsewhere,
// so no Date parsing — same discipline as dateGroups.partsFrom).
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/

function validDate(s: string): boolean {
  const m = DATE_RE.exec(s)
  if (!m) return false
  const month = Number(m[2])
  const day = Number(m[3])
  return month >= 1 && month <= 12 && day >= 1 && day <= 31
}

// A signed decimal number, no exponents/Infinity — keeps the hash human-shaped.
const NUM_RE = /^-?\d+(?:\.\d+)?$/

// Parse a location.hash (with or without the leading '#') into a UrlState.
// Malformed values are dropped FIELD BY FIELD: one bad key never poisons the
// rest of the hash. Duplicate keys: the last occurrence wins.
export function parseHash(hash: string): UrlState {
  const out: UrlState = {}
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  if (!raw) return out
  for (const part of raw.split('&')) {
    const eq = part.indexOf('=')
    if (eq <= 0) continue
    const key = part.slice(0, eq)
    const value = part.slice(eq + 1)
    if (key === 'map') {
      const seg = value.split('/')
      if (seg.length !== 3 || !seg.every((s) => NUM_RE.test(s))) continue
      const zoom = Number(seg[0])
      const latitude = Number(seg[1])
      const longitude = Number(seg[2])
      if (zoom < 0 || zoom > 24) continue
      if (latitude < -90 || latitude > 90) continue
      if (longitude < -180 || longitude > 180) continue
      out.view = { longitude, latitude, zoom }
    } else if (key === 'place') {
      try {
        const p = decodeURIComponent(value)
        if (p) out.place = p
      } catch {
        // malformed %-escape — drop just this field
      }
    } else if (key === 'cap') {
      if (/^\d+$/.test(value)) {
        const id = Number(value)
        if (Number.isSafeInteger(id)) out.cap = id
      }
    } else if (key === 'from' || key === 'to') {
      if (validDate(value)) out[key] = value
    } else if (key === 'bbox') {
      const seg = value.split(',')
      if (seg.length !== 4 || !seg.every((v) => NUM_RE.test(v))) continue
      const [west, south, east, north] = seg.map(Number)
      if (west < -180 || west > 180 || east < -180 || east > 180) continue
      if (south < -90 || south > 90 || north < -90 || north > 90) continue
      // A reversed box would filter the library to nothing — drop it (same
      // rationale as the reversed from/to guard below).
      if (west > east || south > north) continue
      out.bbox = [west, south, east, north]
    } else if (key === 'fav') {
      if (value === '1') out.fav = true
    }
    // unknown keys: ignored
  }
  // A reversed range (from > to; plain string compare, like the range filter
  // itself) would filter the library down to nothing — an inexplicably empty
  // app — so drop BOTH ends rather than trust a hand-mangled hash.
  if (out.from !== undefined && out.to !== undefined && out.from > out.to) {
    delete out.from
    delete out.to
  }
  return out
}

// Serialize to a '#...' hash ('' when nothing is set). Stable key order
// (map, place, cap, from, to, bbox, fav) so equal states always produce equal
// hashes; precision: zoom 2 decimals, lat/lon 5 (~1 m).
export function formatHash(state: UrlState): string {
  const parts: string[] = []
  if (state.view) {
    const { longitude, latitude, zoom } = state.view
    if (Number.isFinite(longitude) && Number.isFinite(latitude) && Number.isFinite(zoom)) {
      // normalizeLon: a world-copy pan (maplibre can report e.g. 190°) still
      // formats as a valid, parseable hash.
      parts.push(
        `map=${zoom.toFixed(2)}/${latitude.toFixed(5)}/${normalizeLon(longitude).toFixed(5)}`,
      )
    }
  }
  if (state.place) parts.push(`place=${encodeURIComponent(state.place)}`)
  if (state.cap !== undefined && Number.isSafeInteger(state.cap) && state.cap >= 0) {
    parts.push(`cap=${state.cap}`)
  }
  if (state.from && validDate(state.from)) parts.push(`from=${state.from}`)
  if (state.to && validDate(state.to)) parts.push(`to=${state.to}`)
  if (state.bbox && state.bbox.every(Number.isFinite)) {
    // 5-decimal rounding can shave ~0.5 m off the box edges; the filter side
    // pads the box (see App's `visible` memo) so edge captures still qualify.
    parts.push(`bbox=${state.bbox.map((v) => v.toFixed(5)).join(',')}`)
  }
  if (state.fav) parts.push('fav=1')
  return parts.length ? `#${parts.join('&')}` : ''
}
