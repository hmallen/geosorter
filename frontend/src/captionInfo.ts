// Pure formatter for the lightbox capture caption: turns a feature's location +
// capture timestamp into a human-readable "Place · Month D, YYYY · h:mm AM/PM" line.
//
// Timezone-stability is the whole point of parsing the string by hand instead of using
// `new Date(...)`: `capture_ts_local` is the capture's LOCAL wall-clock time carried as
// ISO 8601 with the capture-site offset (e.g. '2026-06-13T14:34:22-06:00'). `new Date`
// would re-interpret that instant in the VIEWER's browser timezone and display a shifted
// hour. We read the literal Y/M/D/H/M fields off the string, so the caption always shows
// the time the shutter actually fired.

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

interface CaptionFields {
  place_string: string | null
  capture_ts_local: string | null
  local_date: string | null
}

// 'YYYY-MM-DD' (anchored, ignoring any trailing time) -> 'Month D, YYYY', or null.
function formatDate(ymd: string | undefined): string | null {
  if (!ymd) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd)
  if (!m) return null
  const year = Number(m[1])
  const monthIdx = Number(m[2]) - 1
  const day = Number(m[3])
  if (monthIdx < 0 || monthIdx > 11) return null
  return `${MONTHS[monthIdx]} ${day}, ${year}`
}

// 'HH' + 'MM' (24-hour) -> 'h:mm AM/PM'.
function formatTime(hh: number, mm: number): string {
  const period = hh < 12 ? 'AM' : 'PM'
  const hour12 = hh % 12 === 0 ? 12 : hh % 12
  return `${hour12}:${String(mm).padStart(2, '0')} ${period}`
}

export function captionInfo(props: CaptionFields): string {
  const segments: string[] = []
  if (props.place_string) segments.push(props.place_string)

  // Prefer the full timestamp (date + time); fall back to the date-only local_date.
  const tsMatch = props.capture_ts_local
    ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(props.capture_ts_local)
    : null
  if (tsMatch) {
    const date = formatDate(props.capture_ts_local!)
    if (date) {
      segments.push(date)
      segments.push(formatTime(Number(tsMatch[4]), Number(tsMatch[5])))
    }
  } else {
    const date = formatDate(props.local_date ?? undefined)
    if (date) segments.push(date)
  }

  return segments.length > 0 ? segments.join(' · ') : '—'
}
