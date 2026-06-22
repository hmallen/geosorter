// Pure media-type classification + filter for the FileListPanel's filter chips.
//
// The four user-facing categories overlap in the raw data — a panorama and a
// hyperlapse render are both stored as a `photo`/`video` media_type with a
// `capture_kind`. We collapse each capture into EXACTLY ONE bucket with a fixed
// precedence (panorama > hyperlapse > video > photo) so the filter chips are
// mutually exclusive and a capture is never double-counted.

import type { FeatureProps, LibraryFeature } from './types'

export type MediaCategory = 'photo' | 'video' | 'panorama' | 'hyperlapse'

// All categories in display order (panorama/hyperlapse last so the basic photo/video
// chips lead). The panel seeds its filter with every category enabled.
export const MEDIA_CATEGORIES: MediaCategory[] = ['photo', 'video', 'panorama', 'hyperlapse']

export function categoryOf(props: Pick<FeatureProps, 'capture_kind' | 'media_type'>): MediaCategory {
  if (props.capture_kind === 'panorama') return 'panorama'
  if (props.capture_kind === 'hyperlapse') return 'hyperlapse'
  if (props.media_type === 'video') return 'video'
  return 'photo'
}

export function filterByCategories(
  files: LibraryFeature[],
  enabled: Set<MediaCategory>,
): LibraryFeature[] {
  return files.filter((f) => enabled.has(categoryOf(f.properties)))
}
