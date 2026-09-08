// Pure decision for the lightbox's video-ended event. Manual arrows retain their
// existing wraparound behavior; automatic playback moves forward only inside a flight
// and deliberately stops on its final clip.

import type { TrackScrubPhase } from './flightTrack'

interface SeekTarget {
  token: number
  fileId: number
  timeS: number
}

export type PlaybackSeekRequest = SeekTarget & (
  { kind?: 'scrub'; phase: TrackScrubPhase } |
  { kind: 'marker'; paused: boolean }
)

// Direct jumps can arrive before a new video has metadata. The cleanup cancels
// pending work when navigation supersedes it; the listener applies only once.
export function applyMarkerSeek(
  video: Pick<HTMLVideoElement, 'readyState' | 'duration' | 'currentTime' | 'pause' | 'play' | 'addEventListener' | 'removeEventListener'>,
  timeS: number, paused: boolean, onApplied: (time: number) => void,
): () => void {
  let applied = false
  const apply = () => {
    if (applied || video.readyState < 1) return
    applied = true
    if (paused) video.pause()
    video.currentTime = Math.max(0, Math.min(Number.isFinite(video.duration) ? video.duration : timeS, timeS))
    onApplied(video.currentTime)
    if (!paused) void video.play().catch(() => undefined)
  }
  video.addEventListener('loadedmetadata', apply)
  apply()
  return () => { applied = true; video.removeEventListener('loadedmetadata', apply) }
}

export function nextFlightAutoplayIndex(
  index: number,
  fileCount: number,
  inFlight: boolean,
): number | null {
  if (!inFlight || index < 0 || index >= fileCount - 1) return null
  return index + 1
}
