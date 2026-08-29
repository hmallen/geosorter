// Pure decision for the lightbox's video-ended event. Manual arrows retain their
// existing wraparound behavior; automatic playback moves forward only inside a flight
// and deliberately stops on its final clip.

export function nextFlightAutoplayIndex(
  index: number,
  fileCount: number,
  inFlight: boolean,
): number | null {
  if (!inFlight || index < 0 || index >= fileCount - 1) return null
  return index + 1
}
