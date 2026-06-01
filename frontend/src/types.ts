// Shape of the B6 /api/library GeoJSON feed (see wiki/pages/phase1-backend-api.md).

export interface FeatureProps {
  id: number
  filename: string
  place_string: string | null
  local_date: string | null
  media_type: 'photo' | 'video'
  codec: string | null
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
