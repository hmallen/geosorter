// Pure supercluster wrapper: build an index from the library GeoJSON and query
// clusters/leaves per viewport. Kept side-effect-free so it is unit-testable.
import Supercluster from 'supercluster'
import type { FeatureProps, LibraryFeature } from './types'

export type BBox = [number, number, number, number] // [west, south, east, north]
// getClusters yields point features (props = FeatureProps) or cluster features
// (props = ClusterProperties & AnyProps).
export type ClusterOrPoint =
  | Supercluster.PointFeature<FeatureProps>
  | Supercluster.ClusterFeature<Supercluster.AnyProps>

export function buildIndex(features: LibraryFeature[]): Supercluster<FeatureProps> {
  const index = new Supercluster<FeatureProps>({ radius: 60, maxZoom: 18 })
  index.load(features as unknown as Supercluster.PointFeature<FeatureProps>[])
  return index
}

export function clustersFor(
  index: Supercluster<FeatureProps>,
  bbox: BBox,
  zoom: number,
): ClusterOrPoint[] {
  return index.getClusters(bbox, Math.round(zoom))
}

export function expansionZoom(index: Supercluster<FeatureProps>, clusterId: number): number {
  return index.getClusterExpansionZoom(clusterId)
}

export function leaves(
  index: Supercluster<FeatureProps>,
  clusterId: number,
): Supercluster.PointFeature<FeatureProps>[] {
  return index.getLeaves(clusterId, Infinity)
}
