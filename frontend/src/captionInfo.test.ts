import { describe, expect, it } from 'vitest'
import { captionInfo } from './captionInfo'

describe('captionInfo', () => {
  it('formats place, date, and 12-hour time from capture_ts_local', () => {
    expect(
      captionInfo({
        place_string: 'Estes Park, Colorado, United States',
        capture_ts_local: '2026-06-13T14:34:22-06:00',
        local_date: '2026-06-13',
      }),
    ).toBe('Estes Park, Colorado, United States · June 13, 2026 · 2:34 PM')
  })

  it('renders midnight as 12:00 AM and noon as 12:00 PM', () => {
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-01-01T00:00:00+00:00', local_date: null }),
    ).toBe('January 1, 2026 · 12:00 AM')
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-01-01T12:05:00+00:00', local_date: null }),
    ).toBe('January 1, 2026 · 12:05 PM')
  })

  it('falls back to local_date (date only) when capture_ts_local is null', () => {
    expect(
      captionInfo({
        place_string: 'Boulder, Colorado, United States',
        capture_ts_local: null,
        local_date: '2024-07-04',
      }),
    ).toBe('Boulder, Colorado, United States · July 4, 2024')
  })

  it('renders location only when there is no timestamp at all', () => {
    expect(
      captionInfo({ place_string: 'Boulder, Colorado, United States', capture_ts_local: null, local_date: null }),
    ).toBe('Boulder, Colorado, United States')
  })

  it('renders date + time only when there is no place', () => {
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-06-13T14:34:22-06:00', local_date: '2026-06-13' }),
    ).toBe('June 13, 2026 · 2:34 PM')
  })

  it('returns an em dash when every field is null', () => {
    expect(captionInfo({ place_string: null, capture_ts_local: null, local_date: null })).toBe('—')
  })

  it('reads the literal wall-clock hour from the string regardless of offset (no browser-TZ shift)', () => {
    // Same wall-clock time, two different offsets: both must render 9:15 AM, proving
    // the formatter parses the string's fields and never constructs a Date (which would
    // re-interpret the instant in the viewer's local timezone).
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-03-01T09:15:00-06:00', local_date: null }),
    ).toBe('March 1, 2026 · 9:15 AM')
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-03-01T09:15:00+09:00', local_date: null }),
    ).toBe('March 1, 2026 · 9:15 AM')
  })

  it('tolerates a capture_ts_local with microseconds', () => {
    expect(
      captionInfo({ place_string: null, capture_ts_local: '2026-06-13T14:34:22.123456-06:00', local_date: null }),
    ).toBe('June 13, 2026 · 2:34 PM')
  })

  it('falls back to local_date if capture_ts_local is unparseable', () => {
    expect(
      captionInfo({ place_string: null, capture_ts_local: 'not-a-timestamp', local_date: '2024-07-04' }),
    ).toBe('July 4, 2024')
  })
})
