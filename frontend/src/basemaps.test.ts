import { describe, it, expect } from 'vitest'
import { VECTOR_STYLE, SATELLITE_STYLE, HEATMAP_LAYER, heatmapData } from './basemaps'
import type { LibraryFeature } from './types'

function feat(lon: number, lat: number): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      id: 1, filename: 'x.JPG', place_string: 'P', local_date: '2024-07-04',
      media_type: 'photo', codec: null, gps_source: 'exif', path: 'x.JPG',
      capture_kind: null, frame_count: null, star_rating: null, stitch_status: null,
      stitch_projection: null,
    },
  }
}

describe('basemaps', () => {
  it('VECTOR_STYLE is the hosted vector style URL', () => {
    expect(typeof VECTOR_STYLE).toBe('string')
    expect(VECTOR_STYLE).toMatch(/^https?:\/\//)
  })

  it('SATELLITE_STYLE is an Esri raster style with attribution', () => {
    const sources = SATELLITE_STYLE.sources as Record<string, { type: string; tiles?: string[]; attribution?: string }>
    const raster = Object.values(sources).find((s) => s.type === 'raster')
    expect(raster).toBeDefined()
    expect(raster?.tiles?.[0]).toContain('World_Imagery')
    expect(raster?.attribution && raster.attribution.length).toBeGreaterThan(0)
    const rasterLayers = SATELLITE_STYLE.layers.filter((l) => l.type === 'raster')
    expect(rasterLayers).toHaveLength(1)
  })

  it('HEATMAP_LAYER is a maplibre heatmap layer', () => {
    expect(HEATMAP_LAYER.type).toBe('heatmap')
    expect(HEATMAP_LAYER.id).toBeTruthy()
  })

  it('heatmapData wraps features as a GeoJSON FeatureCollection of points', () => {
    const fc = heatmapData([feat(-105, 40), feat(2, 48)])
    expect(fc.type).toBe('FeatureCollection')
    expect(fc.features).toHaveLength(2)
    expect(fc.features[0].geometry).toEqual({ type: 'Point', coordinates: [-105, 40] })
    expect(fc.features[1].geometry).toEqual({ type: 'Point', coordinates: [2, 48] })
  })
})
