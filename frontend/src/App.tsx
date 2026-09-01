import { useCallback, useMemo, useRef, useState } from 'react'
import MapView from './components/MapView'
import FileListPanel from './components/FileListPanel'
import Lightbox from './components/Lightbox'
import Toolbar from './components/Toolbar'
import QuarantinePanel from './components/QuarantinePanel'
import LocationPanel from './components/LocationPanel'
import TripsPanel from './components/TripsPanel'
import StitchPanel from './components/StitchPanel'
import DuplicatesPanel from './components/DuplicatesPanel'
import RepairPanel from './components/RepairPanel'
import TimelineScrubber from './components/TimelineScrubber'
import { buildPlaces } from './locationFilter'
import { dismissDuplicates, fetchTrack } from './api'
import {
  loadAvailableTracks,
  tracksBBox,
  trackStateAtTime,
  type LoadedVideoTrack,
  type TrackScrubEvent,
} from './flightTrack'
import { buildFlightCatalog, selectionForCatalogFlight } from './flightGroups'
import { filterByDateRange, formatRangeLabel, type DateRange } from './dateRange'
import { effectiveFavorite, filterFavorites } from './favorites'
import type { PlaybackSeekRequest } from './viewerPlayback'
import { parseHash, type UrlState, type UrlView } from './urlState'
import { useLibrary } from './useLibrary'
import { useUrlState } from './useUrlState'
import { useFavorites } from './useFavorites'
import { useRetagJob } from './useRetagJob'
import { useAssignLocation } from './useAssignLocation'
import { useQuarantine } from './useQuarantine'
import { useDuplicates } from './useDuplicates'
import { useStitch } from './useStitch'
import { useStitchAll } from './useStitchAll'
import { useAuthContext } from './useAuth'
import { featuresInBounds } from './viewport'
import type { BBox } from './clusters'
import type { LibraryFeature, QuarantineItem, ViewerSelection } from './types'
import './App.css'

interface ActiveTrackSet {
  contextKey: string
  flight: boolean
  label: string
  tracks: LoadedVideoTrack[]
  totalCount: number
  unavailableCount: number
  currentTime: number
  follow: boolean
  pip: boolean
}

// Build a minimal LibraryFeature from a no-GPS QuarantineItem so its media can be
// previewed in the shared Lightbox. Quarantined captures carry no coordinate, so the
// geometry is a placeholder [0,0] (the Lightbox reads only `properties`, never geometry).
// capture_kind/frame_count are deliberately nulled: the preview is VIEW-ONLY, so the
// Lightbox must not surface the panorama stitch / source-frame controls (whose endpoints
// gate on capture_kind, not status) and let the user start a multi-minute stitch on a
// still-quarantined capture that an assign is about to relocate.
function quarantineToFeature(item: QuarantineItem): LibraryFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: {
      id: item.id,
      filename: item.filename,
      place_string: null,
      local_date: item.date,
      capture_ts_local: null,
      media_type: item.media_type,
      codec: null,
      duration_s: null,
      gps_source: 'none',
      capture_kind: null,
      frame_count: null,
      star_rating: null,
      stitch_status: null,
      stitch_projection: null,
      path: item.path,
    },
  }
}

export default function App() {
  // Admin gate (m-implement-view-only-admin-auth): isAdmin is true when no password
  // is configured (open app) or the user has logged in. The management controls below
  // are passed down only when isAdmin, so a view-only viewer never sees them.
  const { isAdmin, authFetch } = useAuthContext()
  const { features, reload, loading: libraryLoading, error: libraryError } = useLibrary()
  // Shareable URL state (feature 2): the load-time hash, parsed ONCE (lazy
  // initializer) here in App because its fields seed the filter states below;
  // view/place/cap are applied by useUrlState's restore-once effect after the
  // first library load.
  const [initialUrl] = useState<UrlState>(() => parseHash(window.location.hash))
  // Current map viewport bounds, lifted from MapView (null until the map's first
  // onLoad). The side panel always lists the captures inside these bounds.
  const [bounds, setBounds] = useState<BBox | null>(null)
  // The lightbox snapshots the file list it was opened against, so panning the map
  // (which live-updates panelFiles) can't shift its index onto a different file or
  // out of range while it is open.
  const [lightbox, setLightbox] = useState<ViewerSelection | null>(null)
  // Location-filter panel: the distinct-place list (derived client-side) + the
  // panel open flag + the imperative map-fit target. `nonce` makes each pick a
  // distinct value so re-picking the same place re-fires MapView's fitBounds.
  const [showLocations, setShowLocations] = useState(false)
  // Trips panel: auto-derived trips over the library (pure trips.buildTrips);
  // picking one applies its date range as the app-level filter + fits the camera.
  const [showTrips, setShowTrips] = useState(false)
  // Unstitched-panorama panel: a library-wide list of which panorama sets still want a
  // 360 stitch (the toolbar shows only the count).
  const [showStitch, setShowStitch] = useState(false)
  const [flyTo, setFlyTo] = useState<{ bbox: BBox; nonce: number } | null>(null)
  // Fit the camera to a bbox; `nonce` makes each request a distinct value so
  // re-flying to the same bbox re-fires MapView's fitBounds. Stable identity
  // (useCallback) — useUrlState's restore effect depends on it.
  const flyToBBox = useCallback(
    (bbox: BBox) => setFlyTo((p) => ({ bbox, nonce: (p?.nonce ?? 0) + 1 })),
    [],
  )
  const places = useMemo(() => buildPlaces(features), [features])
  // Duplicate-review panel + timeline scrubber visibility (toolbar toggles).
  const [showDuplicates, setShowDuplicates] = useState(false)
  const [showTimeline, setShowTimeline] = useState(false)
  // Broken-capture repair panel (m-repair-broken-captures): scans the quarantine
  // for corrupt files and repairs (untrunc) or deletes them. Admin-only toggle.
  const [showRepair, setShowRepair] = useState(false)
  // App-level view filters (feature 3+4): unlike the panel-local media chips,
  // these apply to BOTH the map and the file list, and are encoded in the hash.
  const [dateRange, setDateRange] = useState<DateRange | null>(
    initialUrl.from && initialUrl.to ? { from: initialUrl.from, to: initialUrl.to } : null,
  )
  // Trip pick's geographic companion to the date range: overlapping-date trips
  // (split by distance in trips.buildTrips) must not bleed into each other's
  // map/file list. Set only by a trip pick; cleared with the chip or by
  // re-scoping the dates from the timeline brush.
  const [tripBBox, setTripBBox] = useState<BBox | null>(initialUrl.bbox ?? null)
  const [favoritesOnly, setFavoritesOnly] = useState(initialUrl.fav === true)
  // Optimistic favorite overrides + the heart toggle (see useFavorites).
  const { favOverrides, toggleFavorite } = useFavorites(authFetch, features)
  // URL-hash breadcrumb: the last place picked in the Locations panel. Never
  // cleared by map gestures — `map=` always wins for camera restore anyway.
  const [pickedPlace, setPickedPlace] = useState<string | null>(initialUrl.place ?? null)
  // The settled camera (moveend cadence), mirrored into the hash. Seeded from
  // the hash so the pre-first-move rewrites don't drop a shared `map=`.
  const [mapView, setMapView] = useState<UrlView | null>(initialUrl.view ?? null)
  // The active route stays owned by App so MapView, the PiP lightbox, and playback
  // synchronization share one source of truth without remounting the video.
  const [track, setTrack] = useState<ActiveTrackSet | null>(null)
  const [trackLoadingKey, setTrackLoadingKey] = useState<string | null>(null)
  const [trackError, setTrackError] = useState<{ contextKey: string; message: string } | null>(null)
  const [playbackSeek, setPlaybackSeek] = useState<PlaybackSeekRequest | null>(null)
  const trackRequest = useRef(0)
  const playbackSeekToken = useRef(0)

  function fitTracks(tracks: LoadedVideoTrack[]) {
    const bbox = tracksBBox(tracks)
    if (bbox) flyToBBox(bbox)
  }

  // Fetch before entering PiP so wholly missing/broken sidecars leave the full viewer
  // intact with an inline error. A flight context loads every usable member track with
  // bounded concurrency; returning from expanded mode reuses the loaded collection.
  async function showTrack() {
    if (!lightbox) return
    const current = lightbox.files[lightbox.index]
    if (!current) return
    const contextKey = lightbox.flight?.key ?? `file:${current.properties.id}`
    if (track?.contextKey === contextKey) {
      setPlaybackSeek(null)
      setTrack((current) => (current ? { ...current, pip: true } : current))
      setTrackError(null)
      fitTracks(track.tracks)
      return
    }
    const request = ++trackRequest.current
    setPlaybackSeek(null)
    setTrackLoadingKey(contextKey)
    setTrackError(null)
    try {
      const candidates = lightbox.flight ? lightbox.files : [current]
      const payload = await loadAvailableTracks(candidates, fetchTrack)
      if (request !== trackRequest.current) return
      if (payload.tracks.length === 0) {
        setTrackError({
          contextKey,
          message: lightbox.flight
            ? 'No usable GPS flight paths were found for this flight.'
            : 'No usable GPS flight track was found in this video’s SRT sidecar.',
        })
        return
      }
      setTrack({
        contextKey,
        flight: lightbox.flight !== null,
        label: lightbox.flight?.label ?? current.properties.filename,
        tracks: payload.tracks,
        totalCount: payload.totalCount,
        unavailableCount: payload.unavailableCount,
        currentTime: 0,
        follow: false,
        pip: true,
      })
      fitTracks(payload.tracks)
    } catch {
      if (request === trackRequest.current) {
        setTrackError({
          contextKey,
          message: lightbox.flight
            ? 'The flight paths could not be loaded. Please try again.'
            : 'The flight track could not be loaded. Please try again.',
        })
      }
    } finally {
      if (request === trackRequest.current) setTrackLoadingKey(null)
    }
  }

  function closeViewer() {
    trackRequest.current += 1
    setTrackLoadingKey(null)
    setTrackError(null)
    setPlaybackSeek(null)
    setTrack(null)
    setLightbox(null)
  }

  const viewerFileId = lightbox?.files[lightbox.index]?.properties.id ?? null
  const viewerContextKey = lightbox
    ? lightbox.flight?.key ?? (viewerFileId === null ? null : `file:${viewerFileId}`)
    : null
  const trackMatchesViewer = Boolean(track && track.contextKey === viewerContextKey)
  const activeVideoTrack = trackMatchesViewer
    ? track?.tracks.find((candidate) => candidate.fileId === viewerFileId) ?? null
    : null
  const syncAvailable = (activeVideoTrack?.samples.length ?? 0) >= 2
  // Position and altitude come from one resolve so the readout can never lag a
  // frame behind the token it sits next to.
  const activeTrackState = useMemo(
    () =>
      track && activeVideoTrack && syncAvailable
        ? trackStateAtTime(activeVideoTrack.samples, track.currentTime)
        : null,
    [track, activeVideoTrack, syncAvailable],
  )
  // Kept as its own memo: MapView's follow effect keys on this array's identity,
  // so it must stay stable across renders that did not move the drone.
  const activeTrackPosition = useMemo(
    () => activeTrackState?.position ?? null,
    [activeTrackState],
  )

  function scrubTrack(event: TrackScrubEvent) {
    if (!activeVideoTrack || !trackMatchesViewer) return
    const fileId = activeVideoTrack.fileId
    setTrack((current) =>
      current && current.contextKey === viewerContextKey
        ? { ...current, currentTime: event.timeS }
        : current,
    )
    setPlaybackSeek({
      token: ++playbackSeekToken.current,
      fileId,
      timeS: event.timeS,
      phase: event.phase,
    })
  }
  // Panorama stitch tracking lives here (above the lightbox) so a ~7-min job's
  // progress survives the lightbox closing/reopening; reload on success so the hero
  // + stitch_status persist on the map and in the panel.
  const { byFile: stitchByFile, start: startStitch } = useStitch(reload)
  // The "Stitch all panoramas" batch action lives here (not in the unmounting
  // StitchPanel) so an in-flight run survives the panel closing/reopening; reload
  // the library on completion so finished panoramas drop out of the target set.
  const stitchAll = useStitchAll(reload)

  // The app-level filter pipeline (spec §3): favorites first, then the date
  // range, then the trip bbox. `visible` feeds BOTH the map and the
  // viewport-filtered panel, so the two surfaces always agree; the media chips
  // stay panel-local as before.
  const visible = useMemo(() => {
    const base = favoritesOnly ? filterFavorites(features, favOverrides) : features
    const dated = filterByDateRange(base, dateRange)
    if (!tripBBox) return dated
    // Pad the box: the hash stores it at 5 decimals (~1 m loss), and the box's
    // edges ARE capture positions — without slack a roundtripped box would drop
    // the very captures that defined it. ~11 m is far below trips' 100 km split.
    const PAD = 1e-4
    return featuresInBounds(dated, [
      tripBBox[0] - PAD,
      tripBBox[1] - PAD,
      tripBBox[2] + PAD,
      tripBBox[3] + PAD,
    ])
  }, [features, favoritesOnly, favOverrides, dateRange, tripBBox])

  // Infer flight identity over the entire app-filtered library BEFORE map bounds are
  // applied. FileListPanel then shows only each group's in-view members, so panning
  // cannot split or renumber a flight merely because an intermediate clip leaves view.
  const flightCatalog = useMemo(() => buildFlightCatalog(visible), [visible])

  // Panel contents: every capture inside the current map viewport. A pure in-memory
  // filter over the already-loaded features — no /api refetch on pan/zoom. Memoized
  // so an unrelated re-render keeps a stable `files` identity for the virtualized
  // grid, and so a pan that doesn't move the settled bounds doesn't re-filter.
  const panelFiles = useMemo(
    () => (bounds ? featuresInBounds(visible, bounds) : []),
    [visible, bounds],
  )

  // Clicking a video marker opens its complete inferred flight, including members
  // outside the settled viewport; non-flight captures retain the viewport-list
  // navigation fallback. A cluster click zooms inside MapView and never reaches here.
  function openInLightbox(id: number) {
    const flightSelection = selectionForCatalogFlight(flightCatalog, id)
    if (flightSelection) {
      setLightbox(flightSelection)
      return
    }
    const idx = panelFiles.findIndex((f) => f.properties.id === id)
    if (idx >= 0) {
      setLightbox({ files: panelFiles, index: idx, flight: null })
      return
    }
    // The marker sits just outside the settled viewport bounds — open it solo.
    const f = features.find((ff) => ff.properties.id === id)
    if (f) setLightbox({ files: [f], index: 0, flight: null })
  }

  // No-GPS (quarantined) captures: the count badges the toolbar button and the list
  // feeds the No-GPS panel. Reloaded by handleChanged after an assign/organize/undo.
  const { items: quarantineItems, count: quarantineCount, reload: reloadQuarantine } =
    useQuarantine()
  const [showNoGps, setShowNoGps] = useState(false)

  // Duplicate-review backlog: the count badges the toolbar button and the list
  // feeds the Duplicates panel. Refreshed by handleChanged (an organize records
  // new rows; an undo can make a former duplicate importable again).
  const { items: duplicateItems, count: duplicatesCount, reload: reloadDuplicates } =
    useDuplicates()

  // After a re-organize / re-tag / assign, close any open lightbox before reloading so
  // it can't keep rendering now-moved files (stale paths → broken media); also refresh
  // the no-GPS list (an assign removes items; an organize may add some) and the
  // duplicate backlog (an organize records rows; a dismiss/undo drains them).
  function handleChanged() {
    trackRequest.current += 1
    setLightbox(null)
    // Files may have moved/left the library; a drawn track could now belong to
    // a re-filed capture, so drop it rather than risk a stale overlay.
    setTrack(null)
    setTrackLoadingKey(null)
    setTrackError(null)
    setPlaybackSeek(null)
    reload()
    reloadQuarantine()
    reloadDuplicates()
  }

  const {
    retag, retagging, placing: retagPlacing,
    beginRetag, cancelRetag, pickLocation: retagPick, clearRetag,
  } = useRetagJob(handleChanged)
  // A re-tag that failed (job error, or a terminal report that isn't 'retagged')
  // must not vanish silently when the "Re-filing…" banner unmounts.
  const retagFailed =
    !retagging &&
    retag !== null &&
    (retag.state === 'error' || (retag.state === 'done' && retag.status !== 'retagged'))
  // Bulk assign-location for no-GPS captures: placement mode (a map click sets the
  // location for the selected captures) coexists with re-tag placement.
  const {
    assign,
    assigning,
    queuedCount,
    queuedFileIds,
    placing: assignPlacing,
    count: assignCount,
    beginAssign,
    cancelAssign,
    pickLocation: assignPick,
    assignToCoord,
  } = useAssignLocation(handleChanged)
  const placing = retagPlacing || assignPlacing
  // One map-click handler routed to whichever placement is active (only one can be).
  const onMapClick = retagPlacing ? retagPick : assignPlacing ? assignPick : undefined

  // Panoramas that still want a 360 stitch: a panorama with tiles whose stitch
  // hasn't succeeded yet. The toolbar's "Stitch all" button + the StitchPanel target
  // these. Memoized on `features` so the per-pan re-renders (setBounds) don't rescan
  // the whole library every time. The full features feed the StitchPanel (it shows a
  // thumbnail + label); the ids feed the toolbar's stitch-all.
  const panoramaTargetFeatures = useMemo(
    () =>
      features.filter(
        (f) =>
          f.properties.capture_kind === 'panorama' &&
          (f.properties.frame_count ?? 0) > 0 &&
          f.properties.stitch_status !== 'ok',
      ),
    [features],
  )
  const panoramaTargets = useMemo(
    () => panoramaTargetFeatures.map((f) => f.properties.id),
    [panoramaTargetFeatures],
  )

  // --- Shareable URL state + lightbox favorite ------------------------------

  const lightboxFile = lightbox ? lightbox.files[lightbox.index] : null
  // The open capture's LIVE library feature — undefined for quarantine previews
  // (client-built features), stitch-panel snapshots of moved files, etc. Drives
  // both the heart's state (the lightbox `files` are a snapshot whose
  // is_favorite goes stale after a reload) and whether the heart is offered at
  // all: favoriting a capture that never resurfaces in any payload (e.g. a
  // quarantined one) would persist server-side as a phantom.
  const lightboxLive = useMemo(
    () =>
      lightboxFile
        ? features.find((f) => f.properties.id === lightboxFile.properties.id)
        : undefined,
    [lightboxFile, features],
  )
  // Effective favorite state for the heart: the live feature's server truth with
  // any optimistic override winning on top.
  const lightboxIsFavorite = useMemo(() => {
    if (!lightboxFile) return false
    return effectiveFavorite((lightboxLive ?? lightboxFile).properties, favOverrides)
  }, [lightboxFile, lightboxLive, favOverrides])

  // Solo lightbox for useUrlState's cap= restore: a shared link points at ONE
  // capture, not a viewport list. Stable identity — the restore effect depends on it.
  const openCapture = useCallback(
    (f: LibraryFeature) => setLightbox({ files: [f], index: 0, flight: null }),
    [],
  )

  // Restore-once of the load-time hash's place/cap + the debounced hash rewrite.
  useUrlState({
    initialUrl,
    features,
    libraryLoading,
    places,
    mapView,
    pickedPlace,
    lightboxFileId: lightboxFile?.properties.id,
    dateRange,
    tripBBox,
    favoritesOnly,
    onFlyTo: flyToBBox,
    onOpenCapture: openCapture,
  })

  return (
    <div className="app">
      <Toolbar
        admin={isAdmin}
        onDone={handleChanged}
        stitchTargets={panoramaTargets}
        onOpenNoGps={() => setShowNoGps((v) => !v)}
        noGpsCount={quarantineCount}
        onOpenLocations={() => setShowLocations((v) => !v)}
        onOpenTrips={() => setShowTrips((v) => !v)}
        onOpenStitch={() => setShowStitch((v) => !v)}
        onOpenDuplicates={() => setShowDuplicates((v) => !v)}
        duplicatesCount={duplicatesCount}
        onOpenRepair={() => setShowRepair((v) => !v)}
        onToggleTimeline={() => setShowTimeline((v) => !v)}
        timelineOn={showTimeline}
        onToggleFavorites={() => setFavoritesOnly((v) => !v)}
        favoritesOn={favoritesOnly}
      />
      <MapView
        features={visible}
        onMarkerClick={placing ? undefined : openInLightbox}
        onMapClick={onMapClick}
        onBoundsChange={setBounds}
        flyTo={flyTo ?? undefined}
        tracks={track?.tracks}
        activeTrackId={activeVideoTrack?.fileId ?? null}
        initialView={initialUrl.view}
        onViewChange={setMapView}
        activeTrackPosition={activeTrackPosition}
        activeTrackTime={track?.currentTime ?? null}
        onTrackScrub={track?.pip && syncAvailable ? scrubTrack : undefined}
        followTrack={Boolean(track?.pip && track.follow && activeTrackPosition)}
        activeTrackAltitude={activeTrackState?.altitude ?? null}
        altitudeRef={activeVideoTrack?.altitudeRef ?? null}
      />
      {/* Under-toolbar chip slot: the flight-track chip plus the active-filter
          chips (date range, favorites) share one horizontal row so they can
          coexist without stacking on top of each other. */}
      {(track?.pip || dateRange || favoritesOnly) && (
        <div className="chips-row">
          {track?.pip && (
            <div className="track-chip">
              <span className="track-chip__label">
                <span className="track-swatch" aria-hidden="true" />
                {track.flight
                  ? `Flight paths — ${track.tracks.length} of ${track.totalCount} videos`
                  : `Flight path — ${track.label}`}
              </span>
              {syncAvailable ? (
                <label className="track-follow">
                  <input
                    type="checkbox"
                    checked={track.follow}
                    onChange={(e) =>
                      setTrack((current) =>
                        current ? { ...current, follow: e.target.checked } : current,
                      )
                    }
                  />
                  Follow drone
                </label>
              ) : (
                <span className="track-sync-warning">Timeline synchronization unavailable</span>
              )}
              {track.unavailableCount > 0 && (
                <span className="track-sync-warning">
                  {track.unavailableCount} path{track.unavailableCount === 1 ? '' : 's'} unavailable
                </span>
              )}
            </div>
          )}
          {(dateRange || tripBBox) && (
            <div className="filter-chip">
              {/* A geographic clamp rides along after a trip pick — say so, and
                  clear both together (a lone invisible bbox would be baffling). */}
              {tripBBox ? 'Trip: ' : ''}
              {dateRange ? formatRangeLabel(dateRange) : 'geographic area'}
              <button
                onClick={() => {
                  setDateRange(null)
                  setTripBBox(null)
                }}
              >
                Clear
              </button>
            </div>
          )}
          {favoritesOnly && (
            <div className="filter-chip">
              ♥ Showing favorites
              <button onClick={() => setFavoritesOnly(false)}>Clear</button>
            </div>
          )}
        </div>
      )}
      {retagPlacing && (
        <div className="placement-banner">
          Click the map to set the new location
          <button onClick={cancelRetag}>Cancel</button>
        </div>
      )}
      {assignPlacing && (
        <div className="placement-banner">
          Click the map to set the location for {assignCount} no-GPS capture
          {assignCount === 1 ? '' : 's'}
          <button onClick={cancelAssign}>Cancel</button>
        </div>
      )}
      {retagging && <div className="placement-banner">Re-filing…</div>}
      {retagFailed && (
        <div className="placement-banner placement-banner--error" role="alert">
          Re-tag failed{retag.error ? ` — ${retag.error}` : retag.status === 'not_found' ? ' — capture not found' : ''}
          <button onClick={clearRetag}>Dismiss</button>
        </div>
      )}
      {/* Initial-load status: without it a failed/slow /api/library read is
          indistinguishable from an empty library (blank world map). Shown only
          before the first successful load — reloads revalidate silently. */}
      {features.length === 0 && libraryLoading && !libraryError && (
        <div className="library-status">
          <span className="spinner" aria-hidden="true" /> Loading library…
        </div>
      )}
      {features.length === 0 && libraryError && !libraryLoading && (
        <div className="library-status library-status--error" role="alert">
          Couldn't load the library.
          <button onClick={reload}>Retry</button>
        </div>
      )}
      {assigning && (
        <div className="placement-banner">
          {assign && assign.total > 0 ? (
            <>
              Assigning location… {assign.processed} of {assign.total}
              <progress value={Math.min(assign.processed, assign.total)} max={assign.total} />
            </>
          ) : (
            'Assigning location…'
          )}
          {queuedCount > 0 && (
            <span>
              {queuedCount} assignment{queuedCount === 1 ? '' : 's'} queued
            </span>
          )}
        </div>
      )}
      {showNoGps && (
        <QuarantinePanel
          items={quarantineItems}
          queuedFileIds={queuedFileIds}
          onClose={() => setShowNoGps(false)}
          onView={(item) => {
            // Open the WHOLE no-GPS list (as view-only features) at the clicked item, so
            // the lightbox prev/next walks every quarantined capture instead of dead-ending.
            const feats = quarantineItems.map(quarantineToFeature)
            const idx = quarantineItems.findIndex((q) => q.id === item.id)
            setLightbox({ files: feats, index: idx < 0 ? 0 : idx, flight: null })
          }}
          onPickOnMap={(ids) => {
            // The two placement modes are mutually exclusive: cancel a pending re-tag
            // so the next map click can't be routed to it (onMapClick prefers re-tag).
            cancelRetag()
            beginAssign(ids)
            setShowNoGps(false)
          }}
          onAssignToPlace={(ids, lat, lon) => assignToCoord(ids, lat, lon)}
        />
      )}
      {showRepair && (
        // Mounted fresh per open, so the panel's auto-scan reruns and a reopened
        // panel never shows a stale broken list.
        <RepairPanel
          busy={assigning}
          onClose={() => setShowRepair(false)}
          onChanged={handleChanged}
        />
      )}
      {showLocations && (
        <LocationPanel
          places={places}
          onClose={() => setShowLocations(false)}
          onPick={(bbox, place) => {
            setPickedPlace(place) // URL-hash breadcrumb (feature 2)
            flyToBBox(bbox)
            setShowLocations(false)
          }}
        />
      )}
      {showTrips && (
        <TripsPanel
          features={features}
          onClose={() => setShowTrips(false)}
          onPick={(trip) => {
            // Fly + filter (user decision): the trip's range becomes the app-level
            // date filter — the existing Clear chip and URL-hash from/to carry it.
            // Two trips split by distance can share dates, so dates alone
            // would show both — the trip's bbox rides along as the geographic
            // filter. The Clear chip and URL-hash from/to/bbox carry the pair.
            setDateRange({ from: trip.from, to: trip.to })
            setTripBBox(trip.bbox)
            flyToBBox(trip.bbox)
            setShowTrips(false)
          }}
        />
      )}
      {showDuplicates && (
        <DuplicatesPanel
          items={duplicateItems}
          onClose={() => setShowDuplicates(false)}
          onDismiss={async (ids) => {
            const res = await dismissDuplicates(authFetch, ids)
            // Dismissal moved inbox files: refresh the backlog (via the shared
            // changed path, which also revalidates the library cheaply).
            handleChanged()
            if (res.failures.length > 0) {
              throw new Error(
                `${res.failures.length} of ${ids.length} could not be dismissed — ` +
                  res.failures[0].error,
              )
            }
          }}
        />
      )}
      {showTimeline && (
        <TimelineScrubber
          features={features}
          range={dateRange}
          onRange={(r) => {
            // Brushing re-scopes the view by TIME — keeping a stale trip bbox
            // would silently hide out-of-area captures inside the new range.
            setTripBBox(null)
            setDateRange(r)
          }}
        />
      )}
      {showStitch && (
        <StitchPanel
          panoramas={panoramaTargetFeatures}
          stitchByFile={stitchByFile}
          onStartStitch={isAdmin ? startStitch : undefined}
          onStitchAll={isAdmin ? () => stitchAll.start(panoramaTargets) : undefined}
          onCancelStitchAll={stitchAll.cancel}
          stitchAllRunning={stitchAll.running}
          stitchAllProgress={stitchAll.progress}
          stitchAllResult={stitchAll.result}
          onView={(f) => {
            // Open the unstitched-panorama list at this capture so prev/next walks them.
            const idx = panoramaTargetFeatures.findIndex(
              (p) => p.properties.id === f.properties.id,
            )
            setLightbox({
              files: panoramaTargetFeatures,
              index: idx < 0 ? 0 : idx,
              flight: null,
            })
          }}
          onClose={() => setShowStitch(false)}
        />
      )}
      <FileListPanel
        files={panelFiles}
        flightCatalog={flightCatalog}
        onOpen={setLightbox}
        activeFlight={
          track?.pip && track.flight
            ? { key: track.contextKey, label: track.label }
            : null
        }
        activeFileId={track?.pip && track.flight ? viewerFileId : null}
        onRevealActiveFlight={
          track?.pip && track.flight ? () => fitTracks(track.tracks) : undefined
        }
        onRetag={
          isAdmin
            ? (file) => {
                // Mutually exclusive with No-GPS assign placement (see onPickOnMap).
                cancelAssign()
                beginRetag(file.properties.id)
              }
            : undefined
        }
      />
      {lightbox && (
        <Lightbox
          files={lightbox.files}
          index={lightbox.index}
          flight={lightbox.flight}
          onIndex={(i) => {
            setPlaybackSeek(null)
            if (lightbox.flight) {
              const nextFileId = lightbox.files[i]?.properties.id
              setTrack((current) => {
                if (!current || current.contextKey !== lightbox.flight?.key) return current
                const nextTrack = current.tracks.find(
                  (candidate) => candidate.fileId === nextFileId,
                )
                return {
                  ...current,
                  currentTime: 0,
                  follow: current.follow && (nextTrack?.samples.length ?? 0) >= 2,
                }
              })
            } else {
              trackRequest.current += 1
              setTrack(null)
              setTrackLoadingKey(null)
              setTrackError(null)
            }
            setLightbox({ ...lightbox, index: i })
          }}
          onClose={closeViewer}
          stitchByFile={stitchByFile}
          onStartStitch={isAdmin ? startStitch : undefined}
          onShowTrack={showTrack}
          isFavorite={lightboxIsFavorite}
          // Heart only for captures that live in the organized library: a
          // quarantine preview / stale snapshot would persist a favorite the UI
          // could never resurface (see lightboxLive above).
          onToggleFavorite={isAdmin && lightboxLive ? toggleFavorite : undefined}
          trackMode={Boolean(track?.pip && trackMatchesViewer)}
          trackActive={trackMatchesViewer}
          trackLoading={trackLoadingKey === viewerContextKey}
          trackError={
            trackError?.contextKey === viewerContextKey
              ? trackError.message
              : null
          }
          trackWarning={
            trackMatchesViewer && track && track.unavailableCount > 0
              ? `${track.unavailableCount} of ${track.totalCount} video path${
                  track.unavailableCount === 1 ? '' : 's'
                } unavailable.`
              : null
          }
          playbackSeek={
            playbackSeek?.fileId === viewerFileId ? playbackSeek : null
          }
          onPlaybackTime={(time) =>
            setTrack((current) =>
              current && current.contextKey === viewerContextKey
                ? { ...current, currentTime: time }
                : current,
            )
          }
          onExpand={() => {
            setPlaybackSeek(null)
            setTrack((current) =>
              current ? { ...current, pip: false, follow: false } : current,
            )
          }}
        />
      )}
    </div>
  )
}
