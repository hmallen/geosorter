// Pure pan/zoom transform math for the flat-panorama hero (FlatHero).
// A flat (non-360) stitched pano is shown inside an overflow:hidden box as an absolutely
// positioned <img> sized `nat * scale` and offset by `transform: translate`. There are no
// scrollbars: at minimum zoom the image fits the box, zooming in is bounded by the native
// 1:1 scale, and panning is a clamped drag. The wheel and a click both drive zoom toward a
// cursor point. The DOM wiring lives in FlatHero.tsx; this module is pure so it is
// unit-testable (jsdom has no layout engine, so the wiring itself is not).

export interface Box {
  width: number
  height: number
}

// The current view transform: `scale` maps image pixels -> screen pixels; (offsetX,offsetY)
// is the image's top-left position within the container, in container pixels.
export interface Transform {
  scale: number
  offsetX: number
  offsetY: number
}

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v))

// Scale at which the image exactly fits the view (object-fit:contain equivalent).
export function fitScale(nat: Box, view: Box): number {
  if (nat.width <= 0 || nat.height <= 0) return 1
  return Math.min(view.width / nat.width, view.height / nat.height)
}

// Maximum zoom: native 1:1, but never below the fit scale (a tiny image that fit-upscales
// past 1:1 can't zoom in further).
export function maxScale(nat: Box, view: Box): number {
  return Math.max(1, fitScale(nat, view))
}

export function clampScale(scale: number, nat: Box, view: Box): number {
  return clamp(scale, fitScale(nat, view), maxScale(nat, view))
}

// Per-axis offset: centre the image when it is smaller than the view on that axis, else
// clamp so its edges cannot be dragged inside the view (no empty gutter).
function axisOffset(offset: number, disp: number, view: number): number {
  if (disp <= view) return (view - disp) / 2
  return clamp(offset, view - disp, 0)
}

export function clampOffset(
  offsetX: number,
  offsetY: number,
  scale: number,
  nat: Box,
  view: Box,
): { offsetX: number; offsetY: number } {
  return {
    offsetX: axisOffset(offsetX, nat.width * scale, view.width),
    offsetY: axisOffset(offsetY, nat.height * scale, view.height),
  }
}

// The fit transform: minimum zoom, centred.
export function fitTransform(nat: Box, view: Box): Transform {
  const scale = fitScale(nat, view)
  const { offsetX, offsetY } = clampOffset(0, 0, scale, nat, view)
  return { scale, offsetX, offsetY }
}

// True when the transform is at (or within rounding tolerance of) the fit scale.
export function isFit(scale: number, nat: Box, view: Box): boolean {
  return scale <= fitScale(nat, view) * 1.0001
}

// Re-zoom to `newScale` (clamped) keeping the image point under the cursor fixed, then
// clamp the offset. (cursorX,cursorY) are container-relative pixels. Used by both the wheel
// and a click.
export function zoomAtPoint(
  t: Transform,
  newScale: number,
  cursorX: number,
  cursorY: number,
  nat: Box,
  view: Box,
): Transform {
  const scale = clampScale(newScale, nat, view)
  const imgX = (cursorX - t.offsetX) / t.scale
  const imgY = (cursorY - t.offsetY) / t.scale
  const { offsetX, offsetY } = clampOffset(cursorX - imgX * scale, cursorY - imgY * scale, scale, nat, view)
  return { scale, offsetX, offsetY }
}

// Pan to startOffset + (dx,dy), clamped. The image follows the pointer (drag right ->
// image moves right), so the delta is added directly.
export function panTo(
  startOffsetX: number,
  startOffsetY: number,
  dx: number,
  dy: number,
  scale: number,
  nat: Box,
  view: Box,
): { offsetX: number; offsetY: number } {
  return clampOffset(startOffsetX + dx, startOffsetY + dy, scale, nat, view)
}
