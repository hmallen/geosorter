import { useCallback, useMemo, useState } from 'react'
import Map, { Layer, Marker, NavigationControl, Source } from 'react-map-gl/maplibre'
import type { MapEvent, MapLayerMouseEvent, ViewStateChangeEvent } from 'react-map-gl/maplibre'
import type { Map as MapLibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type Supercluster from 'supercluster'
import { buildIndex, clustersFor, expansionZoom, type BBox, type ClusterOrPoint } from '../clusters'
import { VECTOR_STYLE, SATELLITE_STYLE, HEATMAP_LAYER, heatmapData } from '../basemaps'
import type { FeatureProps, LibraryFeature } from '../types'

const WORLD: BBox = [-180, -85, 180, 85]

interface Props {
  features: LibraryFeature[]
  onSelect: (index: Supercluster<FeatureProps>, item: ClusterOrPoint) => void
  // Re-tag placement mode (B8): when set, a map-background click reports the
  // clicked coordinate (lng, lat) instead of selecting markers.
  onMapClick?: (lng: number, lat: number) => void
}

export default function MapView({ features, onSelect, onMapClick }: Props) {
  const index = useMemo(() => buildIndex(features), [features])
  const [view, setView] = useState({ longitude: -98, latitude: 39, zoom: 3 })
  const [bbox, setBbox] = useState<BBox>(WORLD)
  const [satellite, setSatellite] = useState(false)
  const [heatmap, setHeatmap] = useState(false)
  const clusters = useMemo(() => clustersFor(index, bbox, view.zoom), [index, bbox, view.zoom])
  const heatData = useMemo(() => heatmapData(features), [features])

  const syncBounds = useCallback((map: MapLibreMap) => {
    const b = map.getBounds()
    setBbox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()])
  }, [])

  return (
    <Map
      longitude={view.longitude}
      latitude={view.latitude}
      zoom={view.zoom}
      onMove={(e: ViewStateChangeEvent) =>
        setView({
          longitude: e.viewState.longitude,
          latitude: e.viewState.latitude,
          zoom: e.viewState.zoom,
        })
      }
      onLoad={(e: MapEvent) => syncBounds(e.target)}
      onMoveEnd={(e: ViewStateChangeEvent) => syncBounds(e.target)}
      onClick={(e: MapLayerMouseEvent) => onMapClick?.(e.lngLat.lng, e.lngLat.lat)}
      cursor={onMapClick ? 'crosshair' : undefined}
      mapStyle={satellite ? SATELLITE_STYLE : VECTOR_STYLE}
      style={{ position: 'absolute', inset: 0 }}
    >
      <NavigationControl position="top-left" />
      <div className="map-controls">
        <label>
          <input type="checkbox" checked={satellite} onChange={(e) => setSatellite(e.target.checked)} />
          Satellite
        </label>
        <label>
          <input type="checkbox" checked={heatmap} onChange={(e) => setHeatmap(e.target.checked)} />
          Heatmap
        </label>
      </div>
      {heatmap && (
        <Source id="library-heat" type="geojson" data={heatData}>
          <Layer {...HEATMAP_LAYER} />
        </Source>
      )}
      {!heatmap && clusters.map((c) => {
        const [lon, lat] = c.geometry.coordinates
        if ('cluster' in c.properties) {
          const { cluster_id, point_count } = c.properties
          return (
            <Marker
              key={`c${cluster_id}`}
              longitude={lon}
              latitude={lat}
              anchor="center"
              onClick={(ev) => {
                ev.originalEvent.stopPropagation()
                setView({ longitude: lon, latitude: lat, zoom: expansionZoom(index, cluster_id) })
                onSelect(index, c)
              }}
            >
              <div className="cluster">{point_count}</div>
            </Marker>
          )
        }
        const props = c.properties
        // Panorama is keyed on capture_kind (its GPS is plain 'exif'), so it must
        // be tested before the gps_source variants (B12).
        const variant =
          props.capture_kind === 'panorama' ? ' pin--panorama'
          : props.gps_source === 'inferred' ? ' pin--inferred'
          : props.gps_source === 'manual' ? ' pin--manual' : ''
        const note =
          props.capture_kind === 'panorama' ? ' (panorama)'
          : props.gps_source === 'inferred' ? ' (inferred location)'
          : props.gps_source === 'manual' ? ' (manually placed)' : ''
        return (
          <Marker
            key={`p${props.id}`}
            longitude={lon}
            latitude={lat}
            anchor="center"
            onClick={(ev) => {
              ev.originalEvent.stopPropagation()
              onSelect(index, c)
            }}
          >
            <div className={`pin${variant}`} title={`${props.filename}${note}`} />
          </Marker>
        )
      })}
      {!heatmap && (
        <div className="map-legend">
          <span><i className="pin pin--legend" /> GPS</span>
          <span><i className="pin pin--inferred pin--legend" /> inferred</span>
          <span><i className="pin pin--manual pin--legend" /> manual</span>
          <span><i className="pin pin--panorama pin--legend" /> panorama</span>
        </div>
      )}
    </Map>
  )
}
