// Basemap styles + the heatmap layer/data for the map viewer (B8 toggles).
// Pure, side-effect-free so it is unit-testable; MapView consumes these.
import type { StyleSpecification, HeatmapLayerSpecification } from 'maplibre-gl'
import type { FeatureCollection, Point } from 'geojson'
import type { LibraryFeature } from './types'

// Default vector basemap (hosted CARTO dark-matter; keyless). Dark to match the
// dark-only UI theme so the map reads as one cohesive dark surface rather than a
// bright rectangle under dark chrome.
export const VECTOR_STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'

// Esri World Imagery — a free raster basemap. Attribution is required and is
// baked into the source so MapLibre renders it in the attribution control.
export const SATELLITE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    esri: {
      type: 'raster',
      tiles: [
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      ],
      tileSize: 256,
      // Cap the source so MapLibre OVERZOOMS (upscales the deepest native tile) for any
      // zoom past 18 instead of requesting tiles Esri may not have. Esri World Imagery's
      // coverage thins out at high zoom in remote areas (this library skews to
      // parks/peaks/hydro), where a missing z19+ tile 404s into a blank/grey void after
      // a cluster click or deep manual zoom. z18 imagery exists essentially everywhere a
      // drone flies, so capping here guarantees no blank tiles — at the cost of slightly
      // soft (upscaled) imagery past z18 even where Esri does have crisp deeper tiles.
      maxzoom: 18,
      attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
    },
  },
  layers: [{ id: 'esri', type: 'raster', source: 'esri' }],
}

// Native MapLibre density heatmap over the library points (source id wired in
// MapView). Radius/opacity tuned for a readable photo-density view.
export const HEATMAP_LAYER: HeatmapLayerSpecification = {
  id: 'library-heat-layer',
  type: 'heatmap',
  source: 'library-heat',
  paint: {
    'heatmap-radius': 30,
    'heatmap-intensity': 1,
    'heatmap-opacity': 0.85,
  },
}

// Wrap the library features as a GeoJSON FeatureCollection for the heatmap source
// (properties are irrelevant to a density layer, so they are dropped).
export function heatmapData(features: LibraryFeature[]): FeatureCollection<Point> {
  return {
    type: 'FeatureCollection',
    features: features.map((f) => ({
      type: 'Feature',
      geometry: f.geometry,
      properties: {},
    })),
  }
}
