import { describe, it, expect } from 'vitest'
import { formatHash, parseHash, type UrlState } from './urlState'

describe('formatHash', () => {
  it('returns an empty string for an empty state', () => {
    expect(formatHash({})).toBe('')
    expect(formatHash({ fav: false })).toBe('')
  })

  it('formats the map view as zoom/lat/lon with fixed precision', () => {
    expect(formatHash({ view: { longitude: -104.98765432, latitude: 39.7392, zoom: 12.3456 } }))
      .toBe('#map=12.35/39.73920/-104.98765')
  })

  it('URL-encodes the place and keeps a stable key order', () => {
    const state: UrlState = {
      fav: true,
      to: '2025-06-30',
      cap: 42,
      place: 'Moab, Utah',
      from: '2025-01-01',
      view: { longitude: -109.5, latitude: 38.5, zoom: 10 },
    }
    expect(formatHash(state)).toBe(
      '#map=10.00/38.50000/-109.50000&place=Moab%2C%20Utah&cap=42&from=2025-01-01&to=2025-06-30&fav=1',
    )
  })

  it('omits absent keys', () => {
    expect(formatHash({ cap: 7 })).toBe('#cap=7')
    expect(formatHash({ fav: true })).toBe('#fav=1')
    expect(formatHash({ from: '2025-01-01', to: '2025-01-31' }))
      .toBe('#from=2025-01-01&to=2025-01-31')
  })

  it('wraps a world-copy longitude back into [-180, 180)', () => {
    expect(formatHash({ view: { longitude: 190, latitude: 0, zoom: 3 } }))
      .toBe('#map=3.00/0.00000/-170.00000')
    expect(formatHash({ view: { longitude: -190, latitude: 0, zoom: 3 } }))
      .toBe('#map=3.00/0.00000/170.00000')
  })

  it('drops a non-finite view and a negative/fractional cap', () => {
    expect(formatHash({ view: { longitude: NaN, latitude: 1, zoom: 2 } })).toBe('')
    expect(formatHash({ cap: -3 })).toBe('')
    expect(formatHash({ cap: 1.5 })).toBe('')
  })

  it('drops malformed from/to values instead of writing garbage', () => {
    expect(formatHash({ from: 'not-a-date', to: '2025-13-40' })).toBe('')
  })

  it('formats the trip bbox at 5 decimals, ordered before fav', () => {
    expect(formatHash({ bbox: [-109.55, 38.567891234, -109.5, 38.6], fav: true }))
      .toBe('#bbox=-109.55000,38.56789,-109.50000,38.60000&fav=1')
  })

  it('drops a non-finite bbox', () => {
    expect(formatHash({ bbox: [NaN, 0, 1, 1] })).toBe('')
  })
})

describe('parseHash', () => {
  it('parses a full hash (with or without the leading #)', () => {
    const h = '#map=10.00/38.50000/-109.50000&place=Moab%2C%20Utah&cap=42&from=2025-01-01&to=2025-06-30&fav=1'
    const want: UrlState = {
      view: { longitude: -109.5, latitude: 38.5, zoom: 10 },
      place: 'Moab, Utah',
      cap: 42,
      from: '2025-01-01',
      to: '2025-06-30',
      fav: true,
    }
    expect(parseHash(h)).toEqual(want)
    expect(parseHash(h.slice(1))).toEqual(want)
  })

  it('returns an empty state for an empty or key-less hash', () => {
    expect(parseHash('')).toEqual({})
    expect(parseHash('#')).toEqual({})
    expect(parseHash('#garbage')).toEqual({})
    expect(parseHash('#=5')).toEqual({})
  })

  it('ignores unknown keys', () => {
    expect(parseHash('#wat=1&cap=3')).toEqual({ cap: 3 })
  })

  it('drops malformed fields individually, keeping the valid ones', () => {
    expect(parseHash('#map=abc&cap=5&fav=1')).toEqual({ cap: 5, fav: true })
    expect(parseHash('#map=10/38.5&cap=5')).toEqual({ cap: 5 }) // missing lon segment
    expect(parseHash('#map=10/38.5/-109.5/9&cap=5')).toEqual({ cap: 5 }) // extra segment
    expect(parseHash('#map=1e3/0/0')).toEqual({}) // exponent form rejected
  })

  it('rejects out-of-range map values', () => {
    expect(parseHash('#map=10/95/-109.5')).toEqual({}) // lat > 90
    expect(parseHash('#map=10/38.5/181')).toEqual({}) // lon > 180
    expect(parseHash('#map=-1/38.5/-109.5')).toEqual({}) // zoom < 0
    expect(parseHash('#map=25/38.5/-109.5')).toEqual({}) // zoom > 24
  })

  it('rejects a non-numeric, negative, or empty cap', () => {
    expect(parseHash('#cap=xyz')).toEqual({})
    expect(parseHash('#cap=-3')).toEqual({})
    expect(parseHash('#cap=')).toEqual({})
  })

  it('rejects malformed dates field-by-field', () => {
    expect(parseHash('#from=2025-13-01&to=2025-06-30')).toEqual({ to: '2025-06-30' })
    expect(parseHash('#from=2025-01-32')).toEqual({})
    expect(parseHash('#from=yesterday')).toEqual({})
  })

  it('drops BOTH ends of a reversed from/to range (would render an empty app)', () => {
    expect(parseHash('#from=2025-06-30&to=2025-01-01')).toEqual({})
    // ...without poisoning the other keys
    expect(parseHash('#from=2025-06-30&to=2025-01-01&cap=5&fav=1')).toEqual({ cap: 5, fav: true })
  })

  it('keeps an equal from/to pair (a valid single-day range)', () => {
    expect(parseHash('#from=2025-03-15&to=2025-03-15'))
      .toEqual({ from: '2025-03-15', to: '2025-03-15' })
  })

  it('still parses a lone from or to as before', () => {
    expect(parseHash('#from=2025-06-30')).toEqual({ from: '2025-06-30' })
    expect(parseHash('#to=2025-01-01')).toEqual({ to: '2025-01-01' })
  })

  it('parses a valid bbox and rejects malformed or out-of-range ones', () => {
    expect(parseHash('#bbox=-109.55000,38.56789,-109.50000,38.60000'))
      .toEqual({ bbox: [-109.55, 38.56789, -109.5, 38.6] })
    expect(parseHash('#bbox=1,2,3')).toEqual({}) // wrong arity
    expect(parseHash('#bbox=a,2,3,4')).toEqual({}) // non-numeric
    expect(parseHash('#bbox=-181,0,0,0')).toEqual({}) // lon out of range
    expect(parseHash('#bbox=0,-91,0,0')).toEqual({}) // lat out of range
    expect(parseHash('#bbox=1,0,0,0')).toEqual({}) // west > east
    expect(parseHash('#bbox=0,1,0,0')).toEqual({}) // south > north
    expect(parseHash('#bbox=1,2,3,4&cap=5')).toEqual({ bbox: [1, 2, 3, 4], cap: 5 })
  })

  it('keeps a degenerate single-point bbox (a one-capture trip)', () => {
    expect(parseHash('#bbox=5,5,5,5')).toEqual({ bbox: [5, 5, 5, 5] })
  })

  it('treats only fav=1 as true', () => {
    expect(parseHash('#fav=1')).toEqual({ fav: true })
    expect(parseHash('#fav=0')).toEqual({})
    expect(parseHash('#fav=true')).toEqual({})
  })

  it('drops a place with a malformed %-escape without poisoning other keys', () => {
    expect(parseHash('#place=%E0%A4%A&cap=9')).toEqual({ cap: 9 })
  })

  it('drops an empty place', () => {
    expect(parseHash('#place=')).toEqual({})
  })

  it('lets the last occurrence of a duplicated key win', () => {
    expect(parseHash('#cap=1&cap=2')).toEqual({ cap: 2 })
  })
})

describe('roundtrip', () => {
  const cases: UrlState[] = [
    {},
    { view: { longitude: -104.98765, latitude: 39.7392, zoom: 12.35 } },
    { place: 'Boulder, Colorado' },
    { cap: 123 },
    { from: '2024-01-01', to: '2024-12-31' },
    { bbox: [-109.55, 38.5, -109.5, 38.6] },
    { fav: true },
    {
      view: { longitude: 0, latitude: 0, zoom: 3 },
      place: 'A & B / C',
      cap: 1,
      from: '2020-02-01',
      to: '2020-02-31',
      bbox: [-75.9, 4.6, -75.5, 5],
      fav: true,
    },
  ]

  it('parse(format(state)) is identity for already-precision-clamped states', () => {
    for (const state of cases) {
      expect(parseHash(formatHash(state))).toEqual(state)
    }
  })

  it('format(parse(hash)) is identity for canonical hashes', () => {
    const h =
      '#map=5.00/10.00000/20.00000&place=X&cap=9&from=2021-03-01&to=2021-04-31&bbox=-75.90000,4.60000,-75.50000,5.00000&fav=1'
    expect(formatHash(parseHash(h))).toBe(h)
  })
})
