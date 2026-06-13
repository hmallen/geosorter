# geosorter map viewer (frontend)

The Phase 1 React + Vite + TypeScript SPA for [geosorter](../README.md): a MapLibre
map that clusters capture-location markers from the backend's `/api/library` GeoJSON
feed and opens the associated photos/videos.

## Stack

- **React 19 + Vite + TypeScript**
- **`react-map-gl`** (MapLibre adapter) + **`maplibre-gl`** — map rendering
- **`supercluster`** — client-side marker clustering
- **OpenFreeMap** — hosted vector tiles (no API key; the only online dependency)
- **Vitest** — unit tests for the pure logic modules

## Develop

The frontend talks to the running backend over HTTP. In two terminals:

```bash
# 1. backend (serves the API on 127.0.0.1:8000)
uv run geosorter serve

# 2. frontend dev server (HMR; proxies /api -> 127.0.0.1:8000)
npm --prefix frontend run dev
```

## Test

```bash
npm --prefix frontend run test   # Vitest: api / clusters / organizeJob / viewport
```

The pure logic (media-URL builders, supercluster wrapper, organize-job polling,
viewport bounds filtering) is unit-tested; the UI and end-to-end flow are verified by a
manual smoke.

## Build (production / same-origin)

```bash
npm --prefix frontend run build  # outputs to ../src/geosorter/webui
uv run geosorter serve           # serves the built SPA at http://127.0.0.1:8000
```

The build output (`src/geosorter/webui/`) is gitignored and produced on demand —
`geosorter serve` mounts it same-origin when present, otherwise it serves a bare API.

## Layout

- `src/api.ts` — typed media-URL builders + `fetchLibrary`
- `src/clusters.ts` — `supercluster` wrapper (`buildIndex`/`clustersFor`/…)
- `src/organizeJob.ts` — `POST /api/organize` + status polling state machine
- `src/viewport.ts` — `featuresInBounds` (pure bounds filter for the side panel)
- `src/useLibrary.ts`, `src/useOrganizeJob.ts` — React hooks
- `src/components/` — `MapView`, `FileListPanel`, `Lightbox`, `Toolbar`
- `src/*.test.ts` — Vitest unit tests (node environment)
