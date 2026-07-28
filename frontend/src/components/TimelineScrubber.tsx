import { useMemo, useRef } from 'react'
import {
  barHeightPct,
  buildTimeline,
  formatRangeLabel,
  handleLeftPct,
  indexFromPointer,
  rangeFromMonths,
  selectionFromRange,
  type DateRange,
} from '../dateRange'
import type { LibraryFeature } from '../types'

interface Props {
  // The FULL library (not the date-filtered view), so the histogram axis stays
  // stable while the user brushes.
  features: LibraryFeature[]
  range: DateRange | null
  onRange: (range: DateRange | null) => void
}

// Floating bottom-center month brush over the library's span (feature 3): month
// bars (heights ∝ count, accent inside the selection), two draggable handles,
// the selected-span label and Clear. All index/percent math lives in the tested
// dateRange.ts module — this component is DOM glue only. Hidden <1024px via CSS
// (the brush needs horizontal room; same breakpoint as the bottom-sheet layout).
export default function TimelineScrubber({ features, range, onRange }: Props) {
  const months = useMemo(() => buildTimeline(features), [features])
  const maxCount = useMemo(
    () => months.reduce((m, b) => Math.max(m, b.count), 0),
    [months],
  )
  const barsRef = useRef<HTMLDivElement | null>(null)
  // Which handle the active pointer drag owns (pointer capture keeps the move
  // stream on the handle even when the cursor leaves the strip). Event-time
  // only — render never reads it (react-hooks/refs).
  const dragRef = useRef<'start' | 'end' | null>(null)

  const sel = selectionFromRange(months, range)
  const pct = handleLeftPct(sel, months.length)
  const shown = range ?? rangeFromMonths(months, sel.start, sel.end)

  // Move one handle to a month index; the other end stays put. Handles can meet
  // (a single-month range) but never cross.
  function moveTo(which: 'start' | 'end', idx: number) {
    const r =
      which === 'start'
        ? rangeFromMonths(months, Math.min(idx, sel.end), sel.end)
        : rangeFromMonths(months, sel.start, Math.max(idx, sel.start))
    if (r) onRange(r)
  }

  function beginDrag(which: 'start' | 'end', e: React.PointerEvent) {
    dragRef.current = which
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  function dragMove(e: React.PointerEvent) {
    const which = dragRef.current
    const strip = barsRef.current
    if (!which || !strip) return
    const rect = strip.getBoundingClientRect()
    moveTo(which, indexFromPointer(e.clientX - rect.left, rect.width, months.length))
  }

  function endDrag() {
    dragRef.current = null
  }

  // Keyboard operation: a focused handle nudges by one month per arrow key.
  function nudge(which: 'start' | 'end', e: React.KeyboardEvent) {
    const idx = which === 'start' ? sel.start : sel.end
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      moveTo(which, idx - 1)
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      moveTo(which, idx + 1)
    }
  }

  if (months.length === 0) return null

  return (
    <div className="timeline-scrubber">
      <div className="timeline-bars" ref={barsRef}>
        {months.map((m, i) => (
          <div
            key={m.key}
            className={`timeline-bar${i >= sel.start && i <= sel.end ? ' timeline-bar--in' : ''}`}
            title={`${m.key}: ${m.count} capture${m.count === 1 ? '' : 's'}`}
          >
            <div
              className="timeline-bar-fill"
              style={{ height: `${barHeightPct(m.count, maxCount)}%` }}
            />
          </div>
        ))}
        <button
          type="button"
          className="timeline-handle"
          role="slider"
          aria-label="Range start"
          aria-valuemin={0}
          aria-valuemax={months.length - 1}
          aria-valuenow={sel.start}
          aria-valuetext={months[sel.start]?.key}
          style={{ left: `${pct.start}%` }}
          onPointerDown={(e) => beginDrag('start', e)}
          onPointerMove={dragMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onKeyDown={(e) => nudge('start', e)}
        />
        <button
          type="button"
          className="timeline-handle"
          role="slider"
          aria-label="Range end"
          aria-valuemin={0}
          aria-valuemax={months.length - 1}
          aria-valuenow={sel.end}
          aria-valuetext={months[sel.end]?.key}
          style={{ left: `${pct.end}%` }}
          onPointerDown={(e) => beginDrag('end', e)}
          onPointerMove={dragMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onKeyDown={(e) => nudge('end', e)}
        />
      </div>
      <div className="timeline-info">
        <span className="timeline-label">{shown ? formatRangeLabel(shown) : ''}</span>
        {range && <button onClick={() => onRange(null)}>Clear</button>}
      </div>
    </div>
  )
}
