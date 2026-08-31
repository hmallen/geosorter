// Pure helpers for the flight-track overlay: bbox + GeoJSON + layer styles for
// a video's GPS path (fetched via api.fetchTrack as [lon, lat] pairs).

import type { LineLayerSpecification } from 'maplibre-gl'
import type { BBox } from './clusters'
import type {
  AltitudeRef,
  FlightTrack,
  FlightTrackSample,
  LibraryFeature,
} from './types'

// Dark casing under a brand-teal line: readable on both the dark vector
// basemap and bright satellite imagery (same rationale as the pin ring).
export const TRACK_CASING_LAYER: LineLayerSpecification = {
  id: 'flight-track-casing',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  filter: ['==', ['get', 'active'], true],
  paint: {
    'line-color': '#0f1116',
    'line-width': 9,
    'line-opacity': 0.9,
  },
}

export const TRACK_LINE_LAYER: LineLayerSpecification = {
  id: 'flight-track-line',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  filter: ['==', ['get', 'active'], true],
  paint: {
    'line-color': '#5eead4', // --accent-text teal
    'line-width': 5,
    'line-opacity': 1,
  },
}

// Context tracks use an opaque light line, dark casing, and a dash pattern so they
// remain legible over detailed satellite imagery while the solid active path is
// still unmistakable.
export const TRACK_INACTIVE_CASING_LAYER: LineLayerSpecification = {
  id: 'flight-track-inactive-casing',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  filter: ['==', ['get', 'active'], false],
  paint: {
    'line-color': '#0f1116',
    'line-width': 7,
    'line-opacity': 0.85,
  },
}

export const TRACK_INACTIVE_LINE_LAYER: LineLayerSpecification = {
  id: 'flight-track-inactive-line',
  type: 'line',
  source: 'flight-track',
  layout: { 'line-cap': 'round', 'line-join': 'round' },
  filter: ['==', ['get', 'active'], false],
  paint: {
    'line-color': '#cffafe',
    'line-width': 3.5,
    'line-opacity': 0.95,
    'line-dasharray': [1.5, 1.1],
  },
}

export interface LoadedVideoTrack extends FlightTrack {
  fileId: number
  filename: string
}

export interface TrackLoadResult {
  tracks: LoadedVideoTrack[]
  totalCount: number
  unavailableCount: number
}

type TrackFetcher = (fileId: number) => Promise<FlightTrack>

// Load only sidecar-backed members with a bounded worker pool. Result order follows
// the flight's chronological file order even when requests finish out of order.
export async function loadAvailableTracks(
  files: LibraryFeature[],
  fetcher: TrackFetcher,
  concurrency = 4,
): Promise<TrackLoadResult> {
  const candidates = files.filter((file) => file.properties.has_track)
  const loaded: Array<LoadedVideoTrack | undefined> = new Array(candidates.length)
  let cursor = 0

  const worker = async () => {
    while (cursor < candidates.length) {
      const slot = cursor
      cursor += 1
      const candidate = candidates[slot]
      try {
        const payload = await fetcher(candidate.properties.id)
        if (payload.points.length < 2) continue
        loaded[slot] = {
          fileId: candidate.properties.id,
          filename: candidate.properties.filename,
          points: payload.points,
          samples: payload.samples,
          altitudeRef: payload.altitudeRef,
        }
      } catch {
        // Partial flight paths are useful; unavailable members are reported by count.
      }
    }
  }

  const limit = Math.max(1, Math.floor(concurrency) || 1)
  await Promise.all(
    Array.from({ length: Math.min(limit, candidates.length) }, () => worker()),
  )
  const tracks = loaded.filter((track): track is LoadedVideoTrack => track !== undefined)
  return {
    tracks,
    totalCount: files.length,
    unavailableCount: files.length - tracks.length,
  }
}

// Bounding box of a track for MapView's fitBounds. A degenerate track (0 or 1
// points) yields null — there is nothing to fit (MapView's maxZoom would cap a
// single-point bbox anyway, but a 1-fix "track" isn't worth a camera move).
export function trackBBox(points: [number, number][]): BBox | null {
  if (points.length < 2) return null
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  for (const [lon, lat] of points) {
    if (lon < west) west = lon
    if (lon > east) east = lon
    if (lat < south) south = lat
    if (lat > north) north = lat
  }
  return [west, south, east, north]
}

// LineString Feature for the maplibre track Source. Kept a plain object (no
// maplibre types) so it stays test-friendly.
export function trackLine(points: [number, number][]): {
  type: 'Feature'
  geometry: { type: 'LineString'; coordinates: [number, number][] }
  properties: Record<string, never>
} {
  return {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: points },
    properties: {},
  }
}

export function trackCollection(
  tracks: LoadedVideoTrack[],
  activeFileId: number | null,
): {
  type: 'FeatureCollection'
  features: Array<{
    type: 'Feature'
    geometry: { type: 'LineString'; coordinates: [number, number][] }
    properties: { fileId: number; filename: string; active: boolean }
  }>
} {
  return {
    type: 'FeatureCollection',
    features: tracks.map((track) => ({
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: track.points },
      properties: {
        fileId: track.fileId,
        filename: track.filename,
        active: track.fileId === activeFileId,
      },
    })),
  }
}

export function tracksBBox(tracks: LoadedVideoTrack[]): BBox | null {
  return trackBBox(tracks.flatMap((track) => track.points))
}

// The drone's telemetry at one instant of the video clock: where it was, and how
// high. `altitude` is null when the surrounding cues carried no height token —
// a sidecar can have GPS without altitude, and a missing height must not hide
// the position marker.
export interface TrackState {
  position: [number, number]
  altitude: number | null
}

export type TrackScrubPhase = 'start' | 'move' | 'end'

export interface TrackScrubEvent {
  phase: TrackScrubPhase
  timeS: number
}

export interface ProjectedTrackSample {
  x: number
  y: number
  timeS: number
}

// Convert a pointer position into the video timestamp at the closest point on
// the projected telemetry route. MapView supplies screen-pixel coordinates so
// "closest" matches the path the user sees regardless of zoom or bearing. At a
// self-crossing (or while the drone is stationary), prefer the candidate nearest
// the current video time so a continuous drag cannot jump to another pass.
export function nearestTrackTime(
  samples: ProjectedTrackSample[],
  target: { x: number; y: number },
  nearTime: number,
): number | null {
  if (samples.length === 0) return null
  if (samples.length === 1) return samples[0].timeS

  let bestDistance = Infinity
  let bestTime = samples[0].timeS
  const tieEpsilon = 1e-7

  for (let index = 0; index < samples.length - 1; index += 1) {
    const left = samples[index]
    const right = samples[index + 1]
    const dx = right.x - left.x
    const dy = right.y - left.y
    const lengthSquared = dx * dx + dy * dy
    let ratio = 0

    if (lengthSquared > Number.EPSILON) {
      ratio = Math.min(
        1,
        Math.max(0, ((target.x - left.x) * dx + (target.y - left.y) * dy) / lengthSquared),
      )
    } else if (Number.isFinite(nearTime) && right.timeS !== left.timeS) {
      // A stationary telemetry span has no spatial clue for its timestamp. Keep
      // the current time when it lies inside the span, otherwise use its nearest
      // temporal endpoint.
      const low = Math.min(left.timeS, right.timeS)
      const high = Math.max(left.timeS, right.timeS)
      const clamped = Math.min(high, Math.max(low, nearTime))
      ratio = (clamped - left.timeS) / (right.timeS - left.timeS)
    }

    const x = left.x + dx * ratio
    const y = left.y + dy * ratio
    const distance = (target.x - x) ** 2 + (target.y - y) ** 2
    const time = left.timeS + (right.timeS - left.timeS) * ratio
    const closerInTime = Math.abs(time - nearTime) < Math.abs(bestTime - nearTime)

    if (distance < bestDistance - tieEpsilon ||
        (Math.abs(distance - bestDistance) <= tieEpsilon && closerInTime)) {
      bestDistance = distance
      bestTime = time
    }
  }

  return bestTime
}

// Interpolate between the two fixes bracketing a value. A null endpoint falls
// back to the other one rather than interpolating toward nothing, so an isolated
// gap in the altitude track holds the last known height instead of blanking.
function lerpOptional(
  left: number | null | undefined,
  right: number | null | undefined,
  ratio: number,
): number | null {
  if (left == null) return right ?? null
  if (right == null) return left
  return left + (right - left) * ratio
}

// Resolve the drone's map position and altitude at a video timestamp. An
// upper-bound binary search intentionally selects the LAST sample at an exact
// duplicate timestamp, then interpolates toward the next later fix.
export function trackStateAtTime(
  samples: FlightTrackSample[],
  timeS: number,
): TrackState | null {
  if (samples.length === 0 || timeS < samples[0].time_s) return null
  const last = samples[samples.length - 1]
  if (timeS >= last.time_s) {
    return { position: [last.lon, last.lat], altitude: last.alt ?? null }
  }

  let low = 0
  let high = samples.length
  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (samples[mid].time_s <= timeS) low = mid + 1
    else high = mid
  }
  const left = samples[Math.max(0, low - 1)]
  const right = samples[low]
  if (!right || right.time_s <= left.time_s) {
    return { position: [left.lon, left.lat], altitude: left.alt ?? null }
  }
  const ratio = (timeS - left.time_s) / (right.time_s - left.time_s)
  return {
    position: [
      left.lon + (right.lon - left.lon) * ratio,
      left.lat + (right.lat - left.lat) * ratio,
    ],
    altitude: lerpOptional(left.alt, right.alt, ratio),
  }
}

// Position-only view of trackStateAtTime, kept for callers (and the map marker)
// that never need the height.
export function positionAtTime(
  samples: FlightTrackSample[],
  timeS: number,
): [number, number] | null {
  return trackStateAtTime(samples, timeS)?.position ?? null
}

// Readout text for the live altitude badge. DJI writes metres; whole metres are
// what a per-frame readout can show without the last digit flickering as noise.
// `-0` is normalized away — a drone at the takeoff plane reads "0 m".
export function formatAltitude(metres: number): string {
  const rounded = Math.round(metres)
  return `${rounded === 0 ? 0 : rounded} m`
}

// Wording for the badge's tooltip/aria label. The datum matters: 120 m above
// takeoff and 120 m above sea level are different flights.
export function altitudeTitle(ref: AltitudeRef | null | undefined): string {
  if (ref === 'relative') return 'Altitude above takeoff'
  if (ref === 'absolute') return 'Altitude above sea level'
  return 'Altitude'
}

export interface PipPosition {
  x: number
  y: number
}

export interface RectSize {
  width: number
  height: number
}

export const PIP_MIN_WIDTH = 280
export const PIP_ASPECT_RATIO = 16 / 9
export const PIP_CHROME_HEIGHT = 50

// Keep a dragged PiP wholly inside the viewport with a small reachable margin.
export function clampPipPosition(
  position: PipPosition,
  pip: RectSize,
  viewport: RectSize,
  margin = 12,
): PipPosition {
  const maxX = Math.max(margin, viewport.width - pip.width - margin)
  const maxY = Math.max(margin, viewport.height - pip.height - margin)
  return {
    x: Math.min(maxX, Math.max(margin, position.x)),
    y: Math.min(maxY, Math.max(margin, position.y)),
  }
}

// Keep a resized PiP inside the viewport while preserving the video's aspect
// ratio. The fixed chrome height accounts for the title bar and resize grip.
export function clampPipWidth(
  width: number,
  position: PipPosition,
  viewport: RectSize,
  margin = 12,
): number {
  const maxByWidth = viewport.width - position.x - margin
  const availableVideoHeight = viewport.height - position.y - margin - PIP_CHROME_HEIGHT
  const maxByHeight = availableVideoHeight * PIP_ASPECT_RATIO
  const maxWidth = Math.max(0, Math.min(maxByWidth, maxByHeight))
  const effectiveMin = Math.min(PIP_MIN_WIDTH, maxWidth)
  return Math.min(maxWidth, Math.max(effectiveMin, width))
}
