import { describe, it, expect } from 'vitest'
import { columnsForWidth, rowCount, rowSlice } from './gridWindow'

describe('columnsForWidth', () => {
  it('fits as many min-width cells (plus gaps) as the panel width allows', () => {
    // (width + gap) / (minCell + gap): 380+8=388 / 128 = 3.03 -> 3
    expect(columnsForWidth(380, 120, 8)).toBe(3)
    // 280+8=288 / 128 = 2.25 -> 2
    expect(columnsForWidth(280, 120, 8)).toBe(2)
    // 900+8=908 / 128 = 7.09 -> 7
    expect(columnsForWidth(900, 120, 8)).toBe(7)
  })

  it('never returns fewer than one column, even for a sliver of width', () => {
    expect(columnsForWidth(40, 120, 8)).toBe(1)
    expect(columnsForWidth(0, 120, 8)).toBe(1)
  })

  it('uses the default min-cell and gap when omitted', () => {
    expect(columnsForWidth(380)).toBe(3)
  })
})

describe('rowCount', () => {
  it('ceils items over columns', () => {
    expect(rowCount(10, 3)).toBe(4) // 3 + 3 + 3 + 1
    expect(rowCount(9, 3)).toBe(3)
    expect(rowCount(1, 3)).toBe(1)
  })

  it('is zero for an empty list', () => {
    expect(rowCount(0, 3)).toBe(0)
  })

  it('treats a non-positive column count as a single column (guard)', () => {
    expect(rowCount(5, 0)).toBe(5)
  })
})

describe('rowSlice', () => {
  it('returns the item index range covered by a row', () => {
    expect(rowSlice(0, 3, 10)).toEqual({ start: 0, end: 3 })
    expect(rowSlice(1, 3, 10)).toEqual({ start: 3, end: 6 })
  })

  it('clamps the last (partial) row to the item count', () => {
    expect(rowSlice(3, 3, 10)).toEqual({ start: 9, end: 10 })
  })
})
