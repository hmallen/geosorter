import { describe, expect, it } from 'vitest'
import {
  clampOffset,
  clampScale,
  fitScale,
  fitTransform,
  isFit,
  maxScale,
  panTo,
  zoomAtPoint,
} from './panZoom'

const WIDE = { width: 4000, height: 1000 }
const VIEW = { width: 800, height: 600 }

describe('fitScale', () => {
  it('is the smaller of the two axis ratios (letterboxed wide image)', () => {
    // min(800/4000=0.2, 600/1000=0.6) = 0.2
    expect(fitScale(WIDE, VIEW)).toBeCloseTo(0.2)
  })
  it('upscales a tiny image to fill the view', () => {
    expect(fitScale({ width: 100, height: 100 }, VIEW)).toBeCloseTo(6) // min(8,6)
  })
})

describe('maxScale', () => {
  it('caps at native 1:1 for a large image', () => {
    expect(maxScale(WIDE, VIEW)).toBe(1)
  })
  it('is the fit scale when even fit upscales past native (tiny image)', () => {
    expect(maxScale({ width: 100, height: 100 }, VIEW)).toBeCloseTo(6)
  })
})

describe('clampScale', () => {
  it('clamps into [fitScale, maxScale]', () => {
    expect(clampScale(0.05, WIDE, VIEW)).toBeCloseTo(0.2) // below fit
    expect(clampScale(5, WIDE, VIEW)).toBe(1) // above native
    expect(clampScale(0.5, WIDE, VIEW)).toBeCloseTo(0.5) // inside range
  })
})

describe('clampOffset', () => {
  it('centres an axis whose displayed size is <= the view', () => {
    // displayed 500x500 in 800x600 -> centred at (150,50)
    expect(clampOffset(999, -999, 1, { width: 500, height: 500 }, VIEW)).toEqual({ offsetX: 150, offsetY: 50 })
  })
  it('clamps an axis larger than the view to [view-disp, 0]', () => {
    // displayed 4000x1000 (scale 1) in 800x600 -> x in [-3200,0], y in [-400,0]
    expect(clampOffset(100, 100, 1, WIDE, VIEW)).toEqual({ offsetX: 0, offsetY: 0 })
    expect(clampOffset(-5000, -5000, 1, WIDE, VIEW)).toEqual({ offsetX: -3200, offsetY: -400 })
  })
})

describe('fitTransform', () => {
  it('is fit scale, centred', () => {
    // scale 0.2 -> displayed 800x200, offset (0, 200)
    const t = fitTransform(WIDE, VIEW)
    expect(t.scale).toBeCloseTo(0.2)
    expect(t.offsetX).toBe(0)
    expect(t.offsetY).toBe(200)
  })
})

describe('isFit', () => {
  it('is true at (or within tolerance of) the fit scale, false when zoomed in', () => {
    expect(isFit(fitScale(WIDE, VIEW), WIDE, VIEW)).toBe(true)
    expect(isFit(1, WIDE, VIEW)).toBe(false)
  })
})

describe('zoomAtPoint', () => {
  const fit = fitTransform(WIDE, VIEW) // { scale:0.2, offsetX:0, offsetY:200 }

  it('keeps the clicked image point under the cursor when zooming to native', () => {
    // cursor (400,300): image point ((400-0)/0.2, (300-200)/0.2) = (2000,500)
    const t = zoomAtPoint(fit, 1, 400, 300, WIDE, VIEW)
    expect(t.scale).toBe(1)
    expect(t.offsetX).toBe(-1600) // 400 - 2000*1
    expect(t.offsetY).toBe(-200) // 300 - 500*1
    // the point really stays under the cursor: offset + img*scale == cursor
    expect(t.offsetX + 2000 * t.scale).toBeCloseTo(400)
    expect(t.offsetY + 500 * t.scale).toBeCloseTo(300)
  })

  it('centres an axis that still fits after a partial zoom', () => {
    // zoom to 0.5: displayed height 500 <= 600, so Y re-centres to 50
    const t = zoomAtPoint(fit, 0.5, 400, 300, WIDE, VIEW)
    expect(t.scale).toBeCloseTo(0.5)
    expect(t.offsetX).toBe(-600) // 400 - 2000*0.5
    expect(t.offsetY).toBe(50) // height 500 < 600 -> centred
  })

  it('clamps the scale to native and re-clamps the offset to the edge', () => {
    const t = zoomAtPoint(fit, 99, 0, 0, WIDE, VIEW)
    expect(t.scale).toBe(1)
    expect(t.offsetX).toBe(0) // clamped to top-left edge
    expect(t.offsetY).toBe(0)
  })
})

describe('panTo', () => {
  it('adds the drag delta to the start offset, clamped', () => {
    expect(panTo(-1600, -200, 50, 60, 1, WIDE, VIEW)).toEqual({ offsetX: -1550, offsetY: -140 })
  })
  it('clamps the panned offset to the valid range', () => {
    expect(panTo(-100, 0, 200, 200, 1, WIDE, VIEW)).toEqual({ offsetX: 0, offsetY: 0 })
  })
})
