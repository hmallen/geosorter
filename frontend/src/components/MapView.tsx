import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, { Layer, Marker, NavigationControl, Source } from 'react-map-gl/maplibre'
import type { MapEvent, MapLayerMouseEvent, ViewStateChangeEvent } from 'react-map-gl/maplibre'
import type { Map as MapLibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { buildIndex, clustersFor, expansionZoom, type BBox } from '../clusters'
import { VECTOR_STYLE, SATELLITE_STYLE, HEATMAP_LAYER, heatmapData } from '../basemaps'
import { trackLine, TRACK_CASING_LAYER, TRACK_LINE_LAYER } from '../flightTrack'
import type { LibraryFeature } from '../types'

const WORLD: BBox = [-180, -85, 180, 85]

interface Props {
  features: LibraryFeature[]
  // Clicking a single capture marker reports its file id so App can open it in the
  // lightbox. A cluster click is handled internally (zoom to expand) and never fires
  // this. Omitted (undefined) during re-tag placement so marker clicks are inert.
  onMarkerClick?: (id: number) => void
  // Re-tag placement mode (B8): when set, a map-background click reports the
  // clicked coordinate (lng, lat) instead of opening a marker.
  onMapClick?: (lng: number, lat: number) => void
  // Viewport-filtered panel: report the settled viewport bounds to App so the
  // side panel can list only captures on screen. Fired on the same
  // onLoad/onMoveEnd cadence as the internal cluster bbox (already debounced).
  onBoundsChange?: (bounds: BBox) => void
  // Location-filter panel: imperatively fit the camera to a place's bounding box.
  // `nonce` makes each pick a distinct value so re-picking the same place re-fires.
  // Done via map.fitBounds (a ref), NOT by lifting the controlled `view` state, so
  // the per-pan re-render stays inside MapView.
  flyTo?: { bbox: BBox; nonce: number }
  // Flight-track overlay: a video's GPS path as [lon, lat] points (App owns the
  // fetch + the dismiss chip). Rendered as a teal line under the markers.
  track?: [number, number][] | null
  // Interpolated video-clock position. Follow mode recenters periodically without
  // changing the user's zoom level.
  activeTrackPosition?: [number, number] | null
  followTrack?: boolean
}

export default function MapView({
  features,
  onMarkerClick,
  onMapClick,
  onBoundsChange,
  flyTo,
  track,
  activeTrackPosition,
  followTrack = false,
}: Props) {
  const index = useMemo(() => buildIndex(features), [features])
  const mapRef = useRef<MapLibreMap | null>(null)
  // The loaded map as STATE (not just a ref) so the moveend-listener effect
  // below re-runs when the map instance appears.
  const [mapObj, setMapObj] = useState<MapLibreMap | null>(null)
  const [view, setView] = useState({ longitude: -98, latitude: 39, zoom: 3 })
  const [bbox, setBbox] = useState<BBox>(WORLD)
  const [satellite, setSatellite] = useState(false)
  const [heatmap, setHeatmap] = useState(false)
  const lastFollowAt = useRef(0)
  const clusters = useMemo(() => clustersFor(index, bbox, view.zoom), [index, bbox, view.zoom])
  const heatData = useMemo(() => heatmapData(features), [features])

  const syncBounds = useCallback((map: MapLibreMap) => {
    const b = map.getBounds()
    const next: BBox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
    setBbox(next)
    onBoundsChange?.(next)
  }, [onBoundsChange])

  // Subscribe to maplibre's `moveend` DIRECTLY on the map, not via react-map-gl's
  // `onMoveEnd` prop. In controlled mode react-map-gl suppresses its own camera
  // callbacks while it applies a programmatic view change (its `_internalUpdate`
  // guard), so `onMoveEnd` never fires for a cluster-click zoom and the panel
  // would stay stale. A direct listener is not gated by that flag, so it fires for
  // BOTH user gestures and programmatic moves — and `getBounds()` is already
  // current because the move has finished. Registered in an effect (with cleanup)
  // so it never leaks and always closes over the CURRENT syncBounds.
  useEffect(() => {
    if (!mapObj) return
    const handler = () => syncBounds(mapObj)
    mapObj.on('moveend', handler)
    return () => {
      mapObj.off('moveend', handler)
    }
  }, [mapObj, syncBounds])

  // Fly to a picked place's bounding box. fitBounds drives onMove -> the direct
  // `moveend` listener (wired above) -> syncBounds, so the panel + clusters
  // refresh for the new viewport. maxZoom caps a single-pin (degenerate) bbox.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !flyTo) return
    map.fitBounds(
      [
        [flyTo.bbox[0], flyTo.bbox[1]],
        [flyTo.bbox[2], flyTo.bbox[3]],
      ],
      { padding: 60, maxZoom: 15, duration: 800 },
    )
  }, [flyTo])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !followTrack || !activeTrackPosition) return
    const now = performance.now()
    if (now - lastFollowAt.current < 250) return
    lastFollowAt.current = now
    map.easeTo({
      center: activeTrackPosition,
      zoom: map.getZoom(),
      duration: 220,
      essential: true,
    })
  }, [activeTrackPosition, followTrack])

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
      onLoad={(e: MapEvent) => {
        const map = e.target
        mapRef.current = map
        setMapObj(map) // arms the moveend-listener effect above
        syncBounds(map)
      }}
      onClick={(e: MapLayerMouseEvent) => onMapClick?.(e.lngLat.lng, e.lngLat.lat)}
      cursor={onMapClick ? 'crosshair' : undefined}
      mapStyle={satellite ? SATELLITE_STYLE : VECTOR_STYLE}
      style={{ position: 'absolute', inset: 0 }}
    >
      <NavigationControl position="top-left" />
      {/* Bottom-left overlay stack: view toggles above the marker legend. Anchored
          here (not top-right) because the file-list panel is a permanent right-edge
          fixture that would otherwise occlude the controls. */}
      <div className="map-overlay-bl">
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
        {!heatmap && (
          <div className="map-legend">
            <span><i className="pin pin--legend" /> GPS</span>
            <span><i className="pin pin--inferred pin--legend" /> inferred</span>
            <span><i className="pin pin--manual pin--legend" /> manual</span>
            <span><i className="pin pin--panorama pin--legend" /> panorama</span>
          </div>
        )}
      </div>
      {heatmap && (
        <Source id="library-heat" type="geojson" data={heatData}>
          <Layer {...HEATMAP_LAYER} />
        </Source>
      )}
      {track && track.length >= 2 && (
        <Source id="flight-track" type="geojson" data={trackLine(track)}>
          <Layer {...TRACK_CASING_LAYER} />
          <Layer {...TRACK_LINE_LAYER} />
        </Source>
      )}
      {/* Takeoff marker: the first GPS fix, echoing the manual-pin green. The
          landing point is just where the line ends — no second marker to
          confuse with the capture's own pin. */}
      {track && track.length >= 2 && (
        <Marker longitude={track[0][0]} latitude={track[0][1]} anchor="center">
          <div className="track-start" title="Takeoff (first GPS fix)" />
        </Marker>
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
                // Controlled camera move. The bounds refresh is handled by the direct
                // maplibre `moveend` listener wired in onLoad (react-map-gl's own
                // onMoveEnd is suppressed for this programmatic change).
                setView({ longitude: lon, latitude: lat, zoom: expansionZoom(index, cluster_id) })
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
              // During re-tag placement onMarkerClick is undefined: don't swallow the
              // click, so it falls through to the map's onMapClick (pickLocation) and
              // a new location can be placed even directly on top of an existing pin.
              if (!onMarkerClick) return
              ev.originalEvent.stopPropagation()
              onMarkerClick(props.id)
            }}
          >
            <div className={`pin${variant}`} title={`${props.filename}${note}`} />
          </Marker>
        )
      })}
      {activeTrackPosition && (
        <Marker
          longitude={activeTrackPosition[0]}
          latitude={activeTrackPosition[1]}
          anchor="center"
        >
          <div className="track-drone" title="Current drone position" aria-label="Current drone position">
            <span aria-hidden="true">✦</span>
          </div>
        </Marker>
      )}
    </Map>
  )
}
