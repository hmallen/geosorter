import { useEffect, useRef, useState } from 'react'
import { formatHash, type UrlState, type UrlView } from './urlState'
import type { DateRange } from './dateRange'
import type { Place } from './locationFilter'
import type { BBox } from './clusters'
import type { LibraryFeature } from './types'

// Shareable URL state (feature 2), one hook per stateful concern (like
// useLibrary / useFavorites). App stays the owner of the domain state (map view,
// filters, lightbox); this hook owns only the URL plumbing around it: the
// one-shot restore of the load-time hash's place/cap, and the debounced rewrite
// of the current state back into the hash. `initialUrl` is parsed once in App
// (a lazy useState initializer) because its fields also SEED App's own filter /
// view states, which must initialize before this hook can receive their values.
interface UseUrlStateArgs {
  initialUrl: UrlState
  features: LibraryFeature[]
  libraryLoading: boolean
  places: Place[]
  mapView: UrlView | null
  pickedPlace: string | null
  lightboxFileId: number | undefined
  dateRange: DateRange | null
  // Trip pick's geographic filter (rides along with its date range).
  tripBBox: BBox | null
  favoritesOnly: boolean
  // Fit the camera to a bbox (App's flyTo channel). Must be identity-stable
  // (useCallback) so the restore effect doesn't churn.
  onFlyTo: (bbox: BBox) => void
  // Open a solo lightbox on this capture (a shared link points at ONE capture,
  // not a viewport list). Must be identity-stable, like onFlyTo.
  onOpenCapture: (feature: LibraryFeature) => void
}

export function useUrlState({
  initialUrl,
  features,
  libraryLoading,
  places,
  mapView,
  pickedPlace,
  lightboxFileId,
  dateRange,
  tripBBox,
  favoritesOnly,
  onFlyTo,
  onOpenCapture,
}: UseUrlStateArgs): void {
  // Apply the hash's place/cap exactly once, after the first library load
  // attempt settles (they need features to resolve against). Deferred a tick:
  // the restore sets React state, which must not happen synchronously inside an
  // effect body (react-hooks/set-state-in-effect); the ref guards the narrow
  // window where a features commit re-runs the effect before `restored` lands.
  const [restored, setRestored] = useState(false)
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restored) return
    if (libraryLoading && features.length === 0) return // first load still in flight
    const id = setTimeout(() => {
      if (restoredRef.current) return
      restoredRef.current = true
      // place: a camera breadcrumb only — an explicit map= always wins.
      if (!initialUrl.view && initialUrl.place) {
        const p = places.find((pl) => pl.place_string === initialUrl.place)
        if (p) onFlyTo(p.bbox)
      }
      if (initialUrl.cap !== undefined) {
        const f = features.find((ff) => ff.properties.id === initialUrl.cap)
        if (f) {
          onOpenCapture(f)
          if (!initialUrl.view) {
            const [lon, lat] = f.geometry.coordinates
            onFlyTo([lon - 0.005, lat - 0.005, lon + 0.005, lat + 0.005])
          }
        }
      }
      setRestored(true)
    }, 0)
    return () => clearTimeout(id)
  }, [restored, libraryLoading, features, places, initialUrl, onFlyTo, onOpenCapture])

  // Debounced hash rewrite: compose the current view/place/capture/filters and
  // replaceState (~300 ms after the last change; never pushes history entries).
  // Gated on `restored` so the initial renders can't strip a shared hash's
  // place/cap before the restore has consumed them.
  useEffect(() => {
    if (!restored) return
    const hash = formatHash({
      view: mapView ?? undefined,
      place: pickedPlace ?? undefined,
      cap: lightboxFileId,
      from: dateRange?.from,
      to: dateRange?.to,
      bbox: tripBBox ?? undefined,
      fav: favoritesOnly || undefined,
    })
    const id = setTimeout(() => {
      history.replaceState(null, '', hash || window.location.pathname + window.location.search)
    }, 300)
    return () => clearTimeout(id)
  }, [restored, mapView, pickedPlace, lightboxFileId, dateRange, tripBBox, favoritesOnly])
}
