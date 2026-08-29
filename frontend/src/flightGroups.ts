// Pure flight inference and nested panel grouping. Flight identity is computed from
// the whole app-filtered library and then reused while the map viewport changes.

import {
  bucketKey,
  bucketLabel,
  parseParts,
  type DateParts,
  type Granularity,
  type RowItem,
  type SortDir,
} from './dateGroups'
import { categoryOf, MEDIA_CATEGORIES, type MediaCategory } from './mediaFilter'
import type { LibraryFeature, ViewerSelection } from './types'

export const FLIGHT_CONTINUITY_MS = 2_000

export interface GroupingFilterState {
  granularity: Granularity
  subgroupFlights: boolean
  enabled: Set<MediaCategory>
  beforeFlight: Set<MediaCategory> | null
}

export interface FlightAssignment {
  key: string
  label: string
  startMs: number | null
  anchorDate: DateParts | null
  totalCount: number
}

export interface FlightGroup extends FlightAssignment {
  members: LibraryFeature[]
}

export interface FlightCatalog {
  assignments: ReadonlyMap<number, FlightAssignment>
  groups: ReadonlyMap<string, FlightGroup>
}

export interface PanelFlightGroup {
  key: string
  label: string
  startMs: number | null
  visibleFiles: LibraryFeature[]
  members: LibraryFeature[]
}

export interface FlightDateGroup {
  key: string
  label: string
  flights: PanelFlightGroup[]
}

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

function singletonGroup(f: LibraryFeature): FlightGroup {
  const stamp = parseLocalStamp(f.properties.capture_ts_local)
  const anchorDate = parseParts(f.properties)
  const base = {
    key: `flight:${f.properties.id}`,
    startMs: stamp?.epochMs ?? null,
    anchorDate,
    totalCount: 1,
    members: [f],
  }
  if (!stamp) {
    return { ...base, label: `Flight time unavailable · ${f.properties.filename}` }
  }
  const at = wallFields(stamp.epochMs, stamp.offsetMinutes)
  return {
    ...base,
    label: `Flight · ${formatDate(at)} · ${formatTime(at)} · duration unavailable`,
  }
}

function addGroup(
  assignments: Map<number, FlightAssignment>,
  groups: Map<string, FlightGroup>,
  group: FlightGroup,
): void {
  groups.set(group.key, group)
  const assignment: FlightAssignment = {
    key: group.key,
    label: group.label,
    startMs: group.startMs,
    anchorDate: group.anchorDate,
    totalCount: group.totalCount,
  }
  for (const member of group.members) assignments.set(member.properties.id, assignment)
}

function finalizeFlight(
  assignments: Map<number, FlightAssignment>,
  groups: Map<string, FlightGroup>,
  flight: FlightAccum,
): void {
  const members = flight.members.map((member) => member.feature)
  addGroup(assignments, groups, {
    key: `flight:${flight.start.feature.properties.id}`,
    label: timedFlightLabel(flight.start, flight.end),
    startMs: flight.start.stamp.epochMs,
    anchorDate: parseParts(flight.start.feature.properties),
    totalCount: members.length,
    members,
  })
}

export function buildFlightCatalog(features: LibraryFeature[]): FlightCatalog {
  const assignments = new Map<number, FlightAssignment>()
  const groups = new Map<string, FlightGroup>()
  const timed: TimedVideo[] = []

  for (const feature of features) {
    if (!ordinaryVideo(feature)) continue
    const stamp = parseLocalStamp(feature.properties.capture_ts_local)
    const duration = feature.properties.duration_s
    if (!stamp || duration === null || !Number.isFinite(duration) || duration <= 0) {
      addGroup(assignments, groups, singletonGroup(feature))
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
    if (current) finalizeFlight(assignments, groups, current)
    current = { members: [video], start: video, end: video, endMs: video.endMs }
  }
  if (current) finalizeFlight(assignments, groups, current)
  return { assignments, groups }
}

// Compatibility view for callers/tests that need only per-file identity.
export function buildFlightIndex(features: LibraryFeature[]): FlightCatalog['assignments'] {
  return buildFlightCatalog(features).assignments
}

function fallbackPanelFlight(file: LibraryFeature): PanelFlightGroup {
  const group = singletonGroup(file)
  return {
    key: group.key,
    label: group.label,
    startMs: group.startMs,
    visibleFiles: [file],
    members: group.members,
  }
}

// Nest the viewport subset beneath Day/Month/Year while retaining every flight's full
// app-filtered member list. A cross-date flight is anchored to its first clip's date.
export function groupFlightsByDate(
  files: LibraryFeature[],
  catalog: FlightCatalog,
  granularity: Granularity,
  dir: SortDir,
): FlightDateGroup[] {
  const panels = new Map<string, PanelFlightGroup>()
  for (const file of files) {
    if (!ordinaryVideo(file)) continue
    const assignment = catalog.assignments.get(file.properties.id)
    const full = assignment ? catalog.groups.get(assignment.key) : undefined
    const existing = assignment ? panels.get(assignment.key) : undefined
    if (existing) existing.visibleFiles.push(file)
    else if (full) {
      panels.set(full.key, {
        key: full.key,
        label: full.label,
        startMs: full.startMs,
        visibleFiles: [file],
        members: full.members,
      })
    } else {
      const fallback = fallbackPanelFlight(file)
      panels.set(fallback.key, fallback)
    }
  }

  for (const panel of panels.values()) {
    const memberOrder = new Map<number, number>()
    panel.members.forEach((member, index) => memberOrder.set(member.properties.id, index))
    panel.visibleFiles.sort((a, b) =>
      (memberOrder.get(a.properties.id) ?? Number.MAX_SAFE_INTEGER) -
        (memberOrder.get(b.properties.id) ?? Number.MAX_SAFE_INTEGER),
    )
  }

  const dated = new Map<string, FlightDateGroup>()
  const undated: PanelFlightGroup[] = []
  for (const panel of panels.values()) {
    const assignment = catalog.assignments.get(panel.members[0].properties.id)
    const parts = assignment?.anchorDate ?? parseParts(panel.members[0].properties)
    if (!parts) {
      undated.push(panel)
      continue
    }
    const key = bucketKey(parts, granularity)
    const existing = dated.get(key)
    if (existing) existing.flights.push(panel)
    else dated.set(key, { key, label: bucketLabel(parts, granularity), flights: [panel] })
  }

  const sign = dir === 'desc' ? -1 : 1
  const compareFlights = (a: PanelFlightGroup, b: PanelFlightGroup) => {
    const idDelta = a.members[0].properties.id - b.members[0].properties.id
    if (a.startMs === null && b.startMs === null) return idDelta
    if (a.startMs === null) return 1
    if (b.startMs === null) return -1
    return (a.startMs - b.startMs) * sign || idDelta
  }
  const result = [...dated.values()].sort((a, b) =>
    a.key < b.key ? -sign : a.key > b.key ? sign : 0,
  )
  for (const group of result) group.flights.sort(compareFlights)
  if (undated.length > 0) {
    undated.sort(compareFlights)
    result.push({ key: '', label: 'Unknown date', flights: undated })
  }
  return result
}

export function buildFlightRowModel(groups: FlightDateGroup[], columns: number): RowItem[] {
  const cols = Math.max(1, Math.floor(columns) || 1)
  const rows: RowItem[] = []
  for (const dateGroup of groups) {
    rows.push({ kind: 'date-header', key: `h:${dateGroup.key}`, label: dateGroup.label })
    for (const flight of dateGroup.flights) {
      rows.push({
        kind: 'flight-header',
        key: `fh:${flight.key}`,
        flightKey: flight.key,
        label: flight.label,
        visibleCount: flight.visibleFiles.length,
        totalCount: flight.members.length,
      })
      const rowCount = Math.ceil(flight.visibleFiles.length / cols)
      for (let offset = 0, row = 0; offset < flight.visibleFiles.length; offset += cols, row += 1) {
        const position = rowCount === 1
          ? 'only'
          : row === 0
            ? 'first'
            : row === rowCount - 1
              ? 'last'
              : 'middle'
        rows.push({
          kind: 'thumbs',
          key: `ft:${flight.key}:${offset}`,
          files: flight.visibleFiles.slice(offset, offset + cols),
          flightKey: flight.key,
          flightPosition: position,
        })
      }
    }
  }
  return rows
}

// Resolve a virtualized row target by flight identity instead of relying on a row
// index that can move when the date grouping, sort direction, or column count changes.
export function flightHeaderRowIndex(rows: RowItem[], flightKey: string): number {
  return rows.findIndex(
    (row) => row.kind === 'flight-header' && row.flightKey === flightKey,
  )
}

export function selectionForFlight(
  flight: Pick<FlightGroup, 'key' | 'label' | 'members'>,
  fileId: number,
): ViewerSelection {
  const index = flight.members.findIndex((member) => member.properties.id === fileId)
  return {
    files: flight.members,
    index: index < 0 ? 0 : index,
    flight: { key: flight.key, label: flight.label },
  }
}

// Resolve a map-marker video through the full app-filtered catalog. Unlike the
// viewport panel, the catalog retains members outside the settled map bounds, so
// opening any member can preserve the complete inferred-flight context.
export function selectionForCatalogFlight(
  catalog: FlightCatalog,
  fileId: number,
): ViewerSelection | null {
  const assignment = catalog.assignments.get(fileId)
  const flight = assignment ? catalog.groups.get(assignment.key) : undefined
  return flight ? selectionForFlight(flight, fileId) : null
}

export function initialGroupingFilterState(): GroupingFilterState {
  return {
    granularity: 'month',
    subgroupFlights: false,
    enabled: new Set(MEDIA_CATEGORIES),
    beforeFlight: null,
  }
}

export function changeGranularity(
  state: GroupingFilterState,
  granularity: Granularity,
): GroupingFilterState {
  return granularity === state.granularity ? state : { ...state, granularity }
}

export function setFlightSubgroups(
  state: GroupingFilterState,
  enabled: boolean,
): GroupingFilterState {
  if (enabled === state.subgroupFlights) return state
  if (enabled) {
    return {
      ...state,
      subgroupFlights: true,
      enabled: new Set<MediaCategory>(['video']),
      beforeFlight: new Set(state.enabled),
    }
  }
  return {
    ...state,
    subgroupFlights: false,
    enabled: new Set(state.beforeFlight ?? MEDIA_CATEGORIES),
    beforeFlight: null,
  }
}

export function toggleGroupingCategory(
  state: GroupingFilterState,
  category: MediaCategory,
): GroupingFilterState {
  if (state.subgroupFlights) return state
  const enabled = new Set(state.enabled)
  if (enabled.has(category)) enabled.delete(category)
  else enabled.add(category)
  return { ...state, enabled }
}
