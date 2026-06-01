// Which files the file-list panel shows when a marker/cluster is clicked.
import type Supercluster from 'supercluster'
import { leaves, type ClusterOrPoint } from './clusters'
import type { FeatureProps, LibraryFeature } from './types'

export function selectionFor(
  index: Supercluster<FeatureProps>,
  item: ClusterOrPoint,
  all: LibraryFeature[],
): LibraryFeature[] {
  if ('cluster' in item.properties) {
    return leaves(index, item.properties.cluster_id) as unknown as LibraryFeature[]
  }
  // A single marker: every file captured at that exact coordinate.
  const [lon, lat] = item.geometry.coordinates
  return all.filter(
    (f) => f.geometry.coordinates[0] === lon && f.geometry.coordinates[1] === lat,
  )
}
