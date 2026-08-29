// Pure flight inference for the viewport file panel. Flight identity is computed from
// the whole app-filtered library and then reused while the map viewport changes.

import type { DateGroup, Granularity, SortDir } from './dateGroups'
import { categoryOf, MEDIA_CATEGORIES, type MediaCategory } from './mediaFilter'
import type { LibraryFeature } from './types'

export const FLIGHT_CONTINUITY_MS = 2_000

export type GroupMode = Granularity | 'flight'

export interface GroupingFilterState {
  mode: GroupMode
  enabled: Set<MediaCategory>
  beforeFlight: Set<MediaCategory> | null
}

export interface FlightAssignment {
  key: string
  label: string
  startMs: number | null
}

export type FlightIndex = ReadonlyMap<number, FlightAssignment>

interface LocalStamp {
  epochMs: number
  offsetMinutes: number
}

interface WallFields {
  year: number
  month: number
  day: number
  hour: number
  minute: number
}

interface TimedVideo {
  feature: LibraryFeature
  stamp: LocalStamp
  endMs: number
}

interface FlightAccum {
  members: TimedVideo[]
  start: TimedVideo
  end: TimedVideo
  endMs: number
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

const LOCAL_ISO =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:\d{2})$/

function ordinaryVideo(f: LibraryFeature): boolean {
  return categoryOf(f.properties) === 'video'
}

// Parse the capture-site offset explicitly instead of relying on browser Date parsing.
// This accepts ExifTool's microseconds and rejects normalized invalid civil dates.
function parseLocalStamp(value: string | null): LocalStamp | null {
  if (!value) return null
  const match = LOCAL_ISO.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const hour = Number(match[4])
  const minute = Number(match[5])
  const second = Number(match[6] ?? 0)
  const millisecond = Number((match[7] ?? '').slice(0, 3).padEnd(3, '0'))
  const zone = match[8]
  let offsetMinutes = 0
  if (zone !== 'Z') {
    const sign = zone[0] === '+' ? 1 : -1
    const offsetHour = Number(zone.slice(1, 3))
    const offsetMinute = Number(zone.slice(4, 6))
    if (offsetHour > 23 || offsetMinute > 59) return null
    offsetMinutes = sign * (offsetHour * 60 + offsetMinute)
  }

  const wallMs = Date.UTC(year, month - 1, day, hour, minute, second, millisecond)
  const check = new Date(wallMs)
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day ||
    check.getUTCHours() !== hour ||
    check.getUTCMinutes() !== minute ||
    check.getUTCSeconds() !== second
  ) return null
  return { epochMs: wallMs - offsetMinutes * 60_000, offsetMinutes }
}

function wallFields(epochMs: number, offsetMinutes: number): WallFields {
  const date = new Date(epochMs + offsetMinutes * 60_000)
  return {
    year: date.getUTCFullYear(),
    month: date.getUTCMonth() + 1,
    day: date.getUTCDate(),
    hour: date.getUTCHours(),
    minute: date.getUTCMinutes(),
  }
}

function formatDate(fields: WallFields): string {
  return `${MONTHS[fields.month - 1]} ${fields.day}, ${fields.year}`
}

function period(fields: WallFields): 'AM' | 'PM' {
  return fields.hour < 12 ? 'AM' : 'PM'
}

function formatTime(fields: WallFields, withPeriod = true): string {
  const hour = fields.hour % 12 === 0 ? 12 : fields.hour % 12
  const base = `${hour}:${String(fields.minute).padStart(2, '0')}`
  return withPeriod ? `${base} ${period(fields)}` : base
}

function sameDate(a: WallFields, b: WallFields): boolean {
  return a.year === b.year && a.month === b.month && a.day === b.day
}

function timedFlightLabel(start: TimedVideo, end: TimedVideo): string {
  const from = wallFields(start.stamp.epochMs, start.stamp.offsetMinutes)
  const to = wallFields(end.endMs, end.stamp.offsetMinutes)
  if (sameDate(from, to)) {
    const range = period(from) === period(to)
      ? `${formatTime(from, false)}–${formatTime(to)}`
      : `${formatTime(from)}–${formatTime(to)}`
    return `Flight · ${formatDate(from)} · ${range}`
  }
  return `Flight · ${formatDate(from)}, ${formatTime(from)} – ${formatDate(to)}, ${formatTime(to)}`
}

function singletonAssignment(f: LibraryFeature): FlightAssignment {
  const stamp = parseLocalStamp(f.properties.capture_ts_local)
  if (!stamp) {
    return {
      key: `flight:${f.properties.id}`,
      label: `Flight time unavailable · ${f.properties.filename}`,
      startMs: null,
    }
  }
  const at = wallFields(stamp.epochMs, stamp.offsetMinutes)
  return {
    key: `flight:${f.properties.id}`,
    label: `Flight · ${formatDate(at)} · ${formatTime(at)} · duration unavailable`,
    startMs: stamp.epochMs,
  }
}

function finalizeFlight(index: Map<number, FlightAssignment>, flight: FlightAccum): void {
  const assignment: FlightAssignment = {
    key: `flight:${flight.start.feature.properties.id}`,
    label: timedFlightLabel(flight.start, flight.end),
    startMs: flight.start.stamp.epochMs,
  }
  for (const member of flight.members) index.set(member.feature.properties.id, assignment)
}

export function buildFlightIndex(features: LibraryFeature[]): FlightIndex {
  const index = new Map<number, FlightAssignment>()
  const timed: TimedVideo[] = []

  for (const feature of features) {
    if (!ordinaryVideo(feature)) continue
    const stamp = parseLocalStamp(feature.properties.capture_ts_local)
    const duration = feature.properties.duration_s
    if (!stamp || duration === null || !Number.isFinite(duration) || duration <= 0) {
      index.set(feature.properties.id, singletonAssignment(feature))
      continue
    }
    timed.push({ feature, stamp, endMs: stamp.epochMs + duration * 1_000 })
  }

  timed.sort((a, b) =>
    a.stamp.epochMs - b.stamp.epochMs || a.feature.properties.id - b.feature.properties.id,
  )

  let current: FlightAccum | null = null
  for (const video of timed) {
    if (current && video.stamp.epochMs <= current.endMs + FLIGHT_CONTINUITY_MS) {
      current.members.push(video)
      if (video.endMs > current.endMs) {
        current.end = video
        current.endMs = video.endMs
      }
      continue
    }
    if (current) finalizeFlight(index, current)
    current = { members: [video], start: video, end: video, endMs: video.endMs }
  }
  if (current) finalizeFlight(index, current)
  return index
}

// Group only the in-view subset, but use assignments inferred from the whole filtered
// library. That keeps keys and boundaries stable while the map pans.
export function groupFlightFeatures(
  files: LibraryFeature[],
  index: FlightIndex,
  dir: SortDir,
): DateGroup[] {
  const groups = new Map<string, DateGroup & { startMs: number | null }>()
  for (const file of files) {
    if (!ordinaryVideo(file)) continue
    const assignment = index.get(file.properties.id) ?? singletonAssignment(file)
    const existing = groups.get(assignment.key)
    if (existing) existing.files.push(file)
    else groups.set(assignment.key, {
      key: assignment.key,
      label: assignment.label,
      files: [file],
      startMs: assignment.startMs,
    })
  }

  for (const group of groups.values()) {
    group.files.sort((a, b) => {
      const aStart = parseLocalStamp(a.properties.capture_ts_local)?.epochMs
      const bStart = parseLocalStamp(b.properties.capture_ts_local)?.epochMs
      if (aStart !== undefined && bStart !== undefined && aStart !== bStart) return aStart - bStart
      if (aStart !== undefined && bStart === undefined) return -1
      if (aStart === undefined && bStart !== undefined) return 1
      return a.properties.id - b.properties.id
    })
  }

  const sign = dir === 'desc' ? -1 : 1
  return [...groups.values()].sort((a, b) => {
    const idDelta = a.files[0].properties.id - b.files[0].properties.id
    if (a.startMs === null && b.startMs === null) return idDelta
    if (a.startMs === null) return 1
    if (b.startMs === null) return -1
    return (a.startMs - b.startMs) * sign || idDelta
  })
}

export function initialGroupingFilterState(): GroupingFilterState {
  return { mode: 'month', enabled: new Set(MEDIA_CATEGORIES), beforeFlight: null }
}

export function changeGroupMode(
  state: GroupingFilterState,
  mode: GroupMode,
): GroupingFilterState {
  if (mode === state.mode) return state
  if (mode === 'flight') {
    return { mode, enabled: new Set<MediaCategory>(['video']), beforeFlight: new Set(state.enabled) }
  }
  if (state.mode === 'flight') {
    return {
      mode,
      enabled: new Set(state.beforeFlight ?? MEDIA_CATEGORIES),
      beforeFlight: null,
    }
  }
  return { ...state, mode }
}

export function toggleGroupingCategory(
  state: GroupingFilterState,
  category: MediaCategory,
): GroupingFilterState {
  if (state.mode === 'flight') return state
  const enabled = new Set(state.enabled)
  if (enabled.has(category)) enabled.delete(category)
  else enabled.add(category)
  return { ...state, enabled }
}
