// Pure favorites helpers (feature 4). The lightbox's `files` array is a SNAPSHOT
// of the library, so after a toggle the server truth (`is_favorite`) can lag the
// user's action; App keeps an optimistic override map (file id -> desired state)
// that wins over the snapshot until fresh features commit.

import type { FeatureProps, LibraryFeature } from './types'

// The displayed favorite state: an override for this id (either direction) beats
// the feature's own is_favorite; absent both, not a favorite.
export function effectiveFavorite(
  props: Pick<FeatureProps, 'id' | 'is_favorite'>,
  overrides: Map<number, boolean>,
): boolean {
  const o = overrides.get(props.id)
  return o !== undefined ? o : props.is_favorite === true
}

// The favorites-only view: keep features whose EFFECTIVE state is favorite, so a
// just-toggled capture appears/disappears immediately (before the reload lands).
export function filterFavorites(
  features: LibraryFeature[],
  overrides: Map<number, boolean>,
): LibraryFeature[] {
  return features.filter((f) => effectiveFavorite(f.properties, overrides))
}
