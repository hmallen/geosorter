// Pure snap math for the mobile bottom-sheet file list (m-implement-mobile-responsive-ui).
// The sheet's height is a fraction of the viewport height; it rests at one of a few
// discrete snap points. The component owns the DOM + pointer events and the live drag
// fraction; these helpers decide where a drag settles and how a tap cycles. Kept DOM-free
// so they are unit-testable in the node-env Vitest suite (mirrors gridWindow.ts).

// Viewport-height fractions the sheet snaps to, ascending:
//   0.12 — collapsed: just the grab handle + header peek above the bottom edge.
//   0.45 — peek: a few thumbnail rows over a still-usable map.
//   0.90 — expanded: near-full-screen browsing grid.
export const SHEET_SNAPS = [0.12, 0.45, 0.9] as const

const MIN_SNAP = SHEET_SNAPS[0]
const MAX_SNAP = SHEET_SNAPS[SHEET_SNAPS.length - 1]

// Clamp a live drag fraction to the usable snap range so the sheet never drags off
// the top of the viewport or below its collapsed handle.
export function clampFraction(fraction: number): number {
  return Math.min(MAX_SNAP, Math.max(MIN_SNAP, fraction))
}

// The snap value closest to a (possibly off-snap) fraction — where a released drag settles.
export function nearestSnap(fraction: number): number {
  const f = clampFraction(fraction)
  let best: number = SHEET_SNAPS[0]
  let bestDist = Math.abs(f - best)
  for (const snap of SHEET_SNAPS) {
    const dist = Math.abs(f - snap)
    if (dist < bestDist) {
      best = snap
      bestDist = dist
    }
  }
  return best
}

// The next snap up from the current resting point, wrapping past the last back to the
// first — drives the tap-to-cycle handle. Off-snap inputs cycle from their nearest snap.
export function cycleSnap(current: number): number {
  const idx = SHEET_SNAPS.indexOf(nearestSnap(current) as (typeof SHEET_SNAPS)[number])
  return SHEET_SNAPS[(idx + 1) % SHEET_SNAPS.length]
}
