// Pure windowing math for the virtualized FileListPanel grid. The component owns
// the DOM (a @tanstack/react-virtual row virtualizer); these helpers compute the
// responsive column count and the item range per row so a row maps to its files.
// Kept DOM-free so they are unit-testable in the node-env Vitest suite.

const DEFAULT_MIN_CELL_PX = 120
const DEFAULT_GAP_PX = 8

// How many fixed-min-width cells (with inter-cell gaps) fit across the panel.
// n cells need n*cell + (n-1)*gap <= width, which rearranges to
// n <= (width + gap) / (cell + gap). Always at least one column.
export function columnsForWidth(
  panelWidthPx: number,
  minCellPx: number = DEFAULT_MIN_CELL_PX,
  gapPx: number = DEFAULT_GAP_PX,
): number {
  const fit = Math.floor((panelWidthPx + gapPx) / (minCellPx + gapPx))
  return Math.max(1, fit)
}

// Number of virtualized rows for an item list at a given column count.
export function rowCount(itemCount: number, columns: number): number {
  const cols = Math.max(1, columns)
  return Math.ceil(itemCount / cols)
}

// The [start, end) item-index range a row covers, clamped to the item count so
// the last (partial) row never overruns.
export function rowSlice(
  rowIndex: number,
  columns: number,
  itemCount: number,
): { start: number; end: number } {
  const cols = Math.max(1, columns)
  const start = rowIndex * cols
  return { start, end: Math.min(start + cols, itemCount) }
}
