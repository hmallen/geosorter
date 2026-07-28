// Shape of the B6 /api/library GeoJSON feed (see wiki/pages/phase1-backend-api.md).

export interface FeatureProps {
  id: number
  filename: string
  place_string: string | null
  local_date: string | null
  // ISO 8601 with local offset (e.g. '2026-06-13T14:34:22-06:00'); the lightbox
  // caption reads its wall-clock fields directly (timezone-stable). null when unknown.
  capture_ts_local: string | null
  media_type: 'photo' | 'video'
  codec: string | null
  gps_source: 'exif' | 'srt' | 'srt_partial' | 'inferred' | 'manual' | 'hyperlapse_frame' | 'none' | null
  capture_kind: 'hyperlapse' | 'panorama' | null // B10: special DJI capture units
  frame_count: number | null // # source frames for a hyperlapse/panorama render
  star_rating: number | null // B11: DJI in-app rating (0..5); null = never rated
  stitch_status: 'pending' | 'ok' | 'failed' | null // B13: panorama hero state; null = none
  // Detected projection of a stitched hero (m-fix-panorama-projection-autodetect):
  // 'equirectangular' -> 360 PanoSphere viewer; 'flat' -> flat zoomable image; null on a
  // legacy hero stitched before this feature (treated as equirectangular by default).
  stitch_projection: 'equirectangular' | 'flat' | null
  // True for a video with an SRT telemetry sidecar — its GPS flight track is
  // drawable via GET /api/track/{id}. Optional: absent on quarantine previews
  // and payloads predating the field.
  has_track?: boolean
  // Favorited (persisted by content hash server-side). Optional: absent on
  // quarantine previews and payloads predating the field. Distinct from
  // star_rating (the DJI in-app rating) — this is the app's own heart toggle.
  is_favorite?: boolean
  path: string // library-relative POSIX path used to build media URLs
}

export interface LibraryFeature {
  type: 'Feature'
  geometry: { type: 'Point'; coordinates: [number, number] } // [lon, lat]
  properties: FeatureProps
}

export interface LibraryFC {
  type: 'FeatureCollection'
  features: LibraryFeature[]
}

export interface FlightTrackSample {
  time_s: number
  lon: number
  lat: number
}

export interface FlightTrack {
  points: [number, number][]
  samples: FlightTrackSample[]
}

// One no-GPS (quarantined) capture awaiting a manual location (GET /api/quarantine).
// These are excluded from /api/library (no coordinate to plot), so the No-GPS panel
// lists them here and assigns a location to promote them to organized.
export interface QuarantineItem {
  id: number
  filename: string
  media_type: 'photo' | 'video'
  date: string | null // local_date, else the _no-gps/<date>/ folder name, else null
  capture_kind: 'hyperlapse' | 'panorama' | null
  frame_count: number | null
  path: string // library-relative POSIX path (still under _no-gps/)
}

// One inbox capture skipped as a duplicate of an already-organized file
// (GET /api/duplicates). With relocate_duplicates off these pile up in the inbox;
// the Duplicates panel lists them and Dismiss moves the group to _duplicates/.
export interface DuplicateItem {
  id: number
  filename: string
  source_path: string // inbox-relative POSIX path of the primary
  matched_path: string | null // library-relative path of the match, null if it died
  matched_file_id: number | null
  sha256: string
  first_seen_at: string
  missing: boolean // source file no longer on disk (dismiss just deletes the row)
}

// One offline forward place-name search match (GET /api/place-search): the user
// picks one to assign its coordinate to the selected no-GPS captures.
export interface PlaceResult {
  geonameid: number
  name: string
  place_string: string | null
  lat: number
  lon: number
  feature_class: string | null
}
