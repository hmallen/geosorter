import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { clampOffset, clampScale, fitTransform, isFit, maxScale, panTo, type Box, type Transform, zoomAtPoint } from '../panZoom'

// Flat zoomable hero for a non-360 stitched panorama
// (m-fix-panorama-projection-autodetect). A 180/wide/vertical pano is NOT a full sphere, so
// it is shown as a plain image (not wrapped onto PanoSphere).
//
// Pan/zoom UX (m-flat-pano-zoom-at-click-pan): a continuous transform model — the image is
// an absolutely-positioned <img> inside an overflow:hidden box, sized `nat*scale` and moved
// by `transform: translate`. There are NEVER scrollbars: at minimum zoom it fits the box,
// the mouse wheel zooms toward the cursor, a click zooms in onto the clicked spot (or back
// to fit), and a drag pans. The pure `panZoom` module does the math; this component is the
// DOM wiring (refs, pointer + wheel events), which jsdom cannot exercise.

// Pointer movement (px) beyond which a press is a drag-pan, not a click-toggle.
const DRAG_THRESHOLD = 4
// Wheel-to-zoom sensitivity: scale *= exp(-deltaY * ZOOM_SPEED).
const ZOOM_SPEED = 0.0015

interface Press {
  startX: number
  startY: number
  startOffsetX: number
  startOffsetY: number
  moved: boolean
}

export default function FlatHero({ src, alt }: { src: string; alt?: string }) {
  // null until the image + container are measured; then drives the absolute-positioned img.
  const [t, setT] = useState<Transform | null>(null)
  const [dragging, setDragging] = useState(false)
  // Measured natural-image + view box, mirrored into state for render-phase reads. The refs
  // below hold the same values for event-time reads (where ref access is allowed); render
  // must read state, not refs (react-hooks/refs), and not force a layout read (viewBox()).
  const [dims, setDims] = useState<{ nat: Box; view: Box } | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const natRef = useRef<Box>({ width: 0, height: 0 })
  const press = useRef<Press | null>(null)

  function viewBox(): Box | null {
    const el = containerRef.current
    if (!el) return null
    return { width: el.clientWidth, height: el.clientHeight }
  }

  // Measure and reset to the fit transform (used on mount, src change, image load, resize).
  function fitNow() {
    const img = imgRef.current
    const view = viewBox()
    if (!img || !view || !img.complete || img.naturalWidth <= 0 || view.width <= 0) return
    natRef.current = { width: img.naturalWidth, height: img.naturalHeight }
    setDims({ nat: natRef.current, view })
    setT(fitTransform(natRef.current, view))
  }

  // On (re)load of a new src: measure now if the image is already decoded (cached), else
  // clear and wait for onLoad. Runs on mount too.
  useLayoutEffect(() => {
    const img = imgRef.current
    if (img && img.complete && img.naturalWidth > 0) fitNow()
    else setT(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src])

  // On container resize (window/lightbox reflow, frame gallery toggling open) KEEP the user's
  // zoom/pan: re-clamp the current transform to the new box. Only fit from scratch when nothing
  // is measured yet — an unconditional fitNow() would snap a zoomed image back to fit.
  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      const view = viewBox()
      if (!view || view.width <= 0) return
      const nat = natRef.current
      if (nat.width <= 0) {
        fitNow()
        return
      }
      setDims((prev) => (prev ? { nat, view } : prev))
      setT((prev) => {
        if (!prev) return prev
        const scale = clampScale(prev.scale, nat, view)
        const { offsetX, offsetY } = clampOffset(prev.offsetX, prev.offsetY, scale, nat, view)
        return { scale, offsetX, offsetY }
      })
    })
    ro.observe(el)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Wheel-to-zoom. A native non-passive listener so preventDefault stops the lightbox from
  // scrolling; functional setT + refs keep it correct without re-binding per render.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const view = viewBox()
      if (!view) return
      const rect = el.getBoundingClientRect()
      const cursorX = e.clientX - rect.left
      const cursorY = e.clientY - rect.top
      const factor = Math.exp(-e.deltaY * ZOOM_SPEED)
      setT((prev) => (prev ? zoomAtPoint(prev, prev.scale * factor, cursorX, cursorY, natRef.current, view) : prev))
      // Keep dims.view mirroring the view t was just computed against (zoom helpers read the
      // live view at event time), so the rendered --zoomed class/cursor stays consistent.
      setDims((prev) => (prev ? { nat: prev.nat, view } : prev))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  function onPointerDown(e: React.PointerEvent) {
    const el = containerRef.current
    if (!el || !t) return
    press.current = { startX: e.clientX, startY: e.clientY, startOffsetX: t.offsetX, startOffsetY: t.offsetY, moved: false }
    el.setPointerCapture(e.pointerId)
    if (!isFit(t.scale, natRef.current, { width: el.clientWidth, height: el.clientHeight })) setDragging(true)
  }

  function onPointerMove(e: React.PointerEvent) {
    const p = press.current
    const view = viewBox()
    if (!p || !view) return
    if (!p.moved && (Math.abs(e.clientX - p.startX) > DRAG_THRESHOLD || Math.abs(e.clientY - p.startY) > DRAG_THRESHOLD)) {
      p.moved = true
    }
    if (!p.moved) return // still within the click threshold — not a pan yet
    const dx = e.clientX - p.startX
    const dy = e.clientY - p.startY
    // Functional update so the pan always composes against the latest scale (a wheel event can
    // interleave with a drag), never the stale render snapshot.
    setT((prev) => {
      if (!prev) return prev
      const { offsetX, offsetY } = panTo(p.startOffsetX, p.startOffsetY, dx, dy, prev.scale, natRef.current, view)
      return { scale: prev.scale, offsetX, offsetY }
    })
  }

  function onPointerUp(e: React.PointerEvent) {
    const el = containerRef.current
    if (el?.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
    const p = press.current
    press.current = null
    setDragging(false)
    if (!p || !el || !t) return
    if (p.moved) return // a pan, not a click — leave the zoom as-is
    const nat = natRef.current
    const view = { width: el.clientWidth, height: el.clientHeight }
    setDims((prev) => (prev ? { nat: prev.nat, view } : prev))
    if (isFit(t.scale, nat, view)) {
      const rect = el.getBoundingClientRect()
      setT(zoomAtPoint(t, maxScale(nat, view), e.clientX - rect.left, e.clientY - rect.top, nat, view))
    } else {
      setT(fitTransform(nat, view))
    }
  }

  function onPointerCancel(e: React.PointerEvent) {
    const el = containerRef.current
    if (el?.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
    press.current = null
    setDragging(false)
  }

  // Render reads measured dims from state (not refs / live layout) so it stays pure and the
  // class/cursor always reflects the transform that produced it.
  const zoomedIn = t !== null && dims !== null && !isFit(t.scale, dims.nat, dims.view)
  const className = `flat-hero${zoomedIn ? ' flat-hero--zoomed' : ''}${dragging ? ' flat-hero--dragging' : ''}`
  const style: React.CSSProperties | undefined =
    t !== null && dims !== null && dims.nat.width > 0
      ? {
          position: 'absolute',
          left: 0,
          top: 0,
          width: dims.nat.width * t.scale,
          height: dims.nat.height * t.scale,
          maxWidth: 'none',
          maxHeight: 'none',
          transform: `translate(${t.offsetX}px, ${t.offsetY}px)`,
        }
      : undefined

  return (
    <div
      ref={containerRef}
      className={className}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      <img
        ref={imgRef}
        src={src}
        alt={alt}
        style={style}
        onLoad={fitNow}
        draggable={false}
        title={zoomedIn ? 'Drag to pan · scroll to zoom · click to fit' : 'Click or scroll to zoom'}
      />
    </div>
  )
}
