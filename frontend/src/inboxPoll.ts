import type { InboxCount } from './api'

// React-free driver for the inbox-badge poll. The in-flight guard fixes the request
// pile-up: while one /api/inbox scan is outstanding (slow over SMB), further ticks are
// dropped instead of stacking dozens of concurrent requests behind the browser's
// 6-connections-per-host cap (which would otherwise starve the organize POST + status
// polls). The guard resets on BOTH resolve and reject so a failed scan never wedges it.
export function createInboxPoll(
  fetchFn: () => Promise<InboxCount>,
  onCount: (c: InboxCount) => void,
  onError?: (e: unknown) => void,
): () => void {
  let inFlight = false
  return function refresh(): void {
    if (inFlight) return
    inFlight = true
    try {
      fetchFn()
        .then(onCount)
        .catch((e) => onError?.(e))
        .finally(() => {
          inFlight = false
        })
    } catch (e) {
      // A fetchFn that throws synchronously (rather than returning a rejected
      // promise) would otherwise wedge the guard forever — reset and report.
      inFlight = false
      onError?.(e)
    }
  }
}
