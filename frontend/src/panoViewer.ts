// Pure viewer-selection for a stitched panorama hero
// (m-fix-panorama-projection-autodetect). A true equirectangular (full 360) hero is
// rendered by the immersive PanoSphere sphere viewer; a non-360 'flat' hero
// (180/wide/vertical) is rendered as a flat zoomable image instead, since wrapping a
// partial pano onto a full sphere maps it incorrectly. A null/undefined/unknown
// projection (a legacy hero stitched before this feature, all of which were full 360)
// defaults to the sphere — so existing 360 panoramas are unaffected.

export type PanoViewer = 'sphere' | 'flat'

export function pickPanoViewer(projection: string | null | undefined): PanoViewer {
  return projection === 'flat' ? 'flat' : 'sphere'
}

// Resolve the viewer from the in-session stitch projection and the library value,
// preferring a *non-empty* in-session projection. On a freshness cache hit the backend
// reports projection '' (no new HFOV) and then backfills files.stitch_projection, so the
// empty in-session value must NOT mask the backfilled library value after the reload —
// hence `||` (treats '' as absent), not `??` (which would keep the empty string).
export function resolvePanoViewer(
  sessionProjection: string | null | undefined,
  libraryProjection: string | null | undefined,
): PanoViewer {
  return pickPanoViewer(sessionProjection || libraryProjection)
}
