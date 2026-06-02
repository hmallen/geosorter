import { useCallback, useMemo, useState } from 'react'
import Map, { Marker, NavigationControl } from 'react-map-gl/maplibre'
import type { MapEvent, MapLayerMouseEvent, ViewStateChangeEvent } from 'react-map-gl/maplibre'
import type { Map as MapLibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type Supercluster from 'supercluster'
import { buildIndex, clustersFor, expansionZoom, type BBox, type ClusterOrPoint } from '../clusters'
import type { FeatureProps, LibraryFeature } from '../types'

const STYLE = 'https://tiles.openfreemap.org/styles/liberty'
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
  const clusters = useMemo(() => clustersFor(index, bbox, view.zoom), [index, bbox, view.zoom])

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
      mapStyle={STYLE}
      style={{ position: 'absolute', inset: 0 }}
    >
      <NavigationControl position="top-left" />
      {clusters.map((c) => {
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
        const variant =
          props.gps_source === 'inferred' ? ' pin--inferred'
          : props.gps_source === 'manual' ? ' pin--manual' : ''
        const note =
          props.gps_source === 'inferred' ? ' (inferred location)'
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
      <div className="map-legend">
        <span><i className="pin pin--legend" /> GPS</span>
        <span><i className="pin pin--inferred pin--legend" /> inferred</span>
        <span><i className="pin pin--manual pin--legend" /> manual</span>
      </div>
    </Map>
  )
}
