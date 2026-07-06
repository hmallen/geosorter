import { useState } from 'react'
import { setFavorite } from './api'
import type { LibraryFeature } from './types'

// Optimistic favorites state (feature 4), one hook per stateful concern (like
// useLibrary / useQuarantine). Overrides are keyed to the features array they
// were made against: a fresh features commit changes that identity, which
// implicitly discards stale overrides — no state-clearing effect (and no extra
// render).

// Stable empty-override map: returned while the stored overrides belong to a
// superseded features array, so consumers get a constant identity (no churn).
const NO_OVERRIDES: Map<number, boolean> = new Map()

export function useFavorites(authFetch: typeof fetch, features: LibraryFeature[]) {
  const [favState, setFavState] = useState<{
    base: LibraryFeature[] | null
    map: Map<number, boolean>
  }>({ base: null, map: NO_OVERRIDES })
  const favOverrides = favState.base === features ? favState.map : NO_OVERRIDES

  // Optimistic heart toggle: record the override for instant UI, then POST.
  // Deliberately NO reload on success — the toggle always busts the library
  // ETag, so a reload would re-download the whole library on every click; the
  // override already shows the new state and stays valid until the next natural
  // features commit, which carries the server truth. On failure, revert — there
  // is no global error-banner pattern for non-job actions, so log and restore
  // the old state.
  async function toggleFavorite(id: number, next: boolean) {
    setFavState((prev) => {
      const map = new Map(prev.base === features ? prev.map : NO_OVERRIDES)
      map.set(id, next)
      return { base: features, map }
    })
    try {
      await setFavorite(authFetch, id, next)
    } catch (e) {
      console.error('favorite toggle failed', e)
      setFavState((prev) => {
        if (prev.base !== features) return prev
        const map = new Map(prev.map)
        map.delete(id)
        return { base: prev.base, map }
      })
    }
  }

  return { favOverrides, toggleFavorite }
}
