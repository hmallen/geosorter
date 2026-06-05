// Bounded backoff for re-requesting a derived-asset image (thumbnail / preview /
// poster) that errors while it is still being generated server-side on first
// request — over a slow library volume the first GET can 404/stall, then succeed
// once Pillow/ffmpeg has warmed the cache. Pure so it can be unit-tested.

export const MAX_IMG_RETRIES = 4

// attempt 1 -> 400ms, doubling, capped at 4s: 400, 800, 1600, 3200, then 4000.
export function retryDelayMs(attempt: number): number {
  return Math.min(400 * 2 ** (attempt - 1), 4000)
}
