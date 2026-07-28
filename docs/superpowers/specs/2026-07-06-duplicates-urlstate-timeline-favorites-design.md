# Design: duplicate review, shareable URLs, date-range scrubber, favorites

Date: 2026-07-06. Branch: `fable-features`.

Four features, designed against the current code (schema v4, `lib3-` library ETag,
no frontend routing). Each section states the decision and the contract; rationale
inline where a judgment call was made.

## 1. Duplicate-review panel

**Problem.** With `relocate_duplicates = false`, a detected duplicate stays in the
inbox and nothing is persisted, so every organize run re-hashes and re-skips it
forever. There is no way to see or drain that backlog short of flipping the config.

**Persistence.** New index-DB table (created via `_INDEX_SCHEMA`'s
`CREATE TABLE IF NOT EXISTS`; new tables need no ALTER migration):

```sql
CREATE TABLE IF NOT EXISTS duplicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,      -- absolute inbox path of the primary
    sha256 TEXT NOT NULL,
    companion_paths TEXT NOT NULL DEFAULT '[]',  -- JSON array of absolute paths
    matched_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    matched_dest_path TEXT,                -- snapshot for display if the row dies
    batch_id TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`SCHEMA_VERSION` bumps 4 → 5 (also covers the favorites table below).

**Recording (organize.py `_process_group`).** At the existing
`duplicates_skipped` site:

- `relocate_duplicates` off: upsert a `duplicates` row
  (`ON CONFLICT(source_path) DO UPDATE` refreshing sha256/companions/match/batch).
- `relocate_duplicates` on: the physical move to `_duplicates/` is the record, as
  today; additionally delete any stale `duplicates` row for that source_path.
- When a group is *imported* normally (not a duplicate), delete any stale
  `duplicates` row for its primary path — covers the case where the matched
  library file was undone and the former duplicate becomes importable.

**Dismissal semantics.** Dismiss = move the capture group (companions first,
primary last) into `<inbox>/_duplicates/`, preserving inbox-relative subpaths
with `_2`/`_3` collision suffixes, append the same TSV line to
`duplicates.log`, then delete the row. This drains the backlog without deleting
user data and reuses the exact mechanism `relocate_duplicates=on` uses; the
`_duplicates/` scan exclusion keeps them gone. A row whose source file no longer
exists on disk is dismissed by just deleting the row.

The move/log/target logic moves out of `organize.py` into a new module
`src/geosorter/duplicates.py` (record, list, dismiss, `_dup_target`); organize
imports it, so there is one implementation.

**API** (mirrors the quarantine pattern):

- `GET /api/duplicates` (public read) →
  `{"items": [{"id", "filename", "source_path" (inbox-relative, POSIX),
  "matched_path" (library-relative or null), "matched_file_id", "sha256",
  "first_seen_at", "missing" (bool, source gone from disk)}], "count": N}`.
- `POST /api/duplicates/dismiss` (guarded, `Depends(require_admin)`), body
  `{"ids": [int]}` → moves files + deletes rows synchronously (renames are
  cheap; no job needed). Returns `{"dismissed": n, "failures": [{"id", "error"}]}`.
  Unknown ids are counted in `failures`? No — skipped silently like
  `assign_locations` skips non-quarantined ids; response includes `"skipped": n`.
  Returns **409** with `{message, blocking_job_id}` if any destructive job
  (including organize) is running — moving inbox files under a live organize is
  racy. `JobManager` gains a public `active_destructive_job_id()` accessor.

**Frontend.** `useDuplicates` hook (mirrors `useQuarantine`); Toolbar button
`Duplicates (N)` shown when `admin && count > 0`; `DuplicatesPanel` styled like
QuarantinePanel (top-left panel, `.panel-head`, list rows showing filename,
matched library path, first-seen date, a per-row Dismiss button and a
"Dismiss all" header action). Refetches after organize/undo runs (same
`handleChanged` path as quarantine).

## 2. Shareable URL state

**Format.** URL hash, query-style, parsed once on load, written with
`history.replaceState` (debounced ~300 ms; no history-stack spam):

```
#map=<zoom>/<lat>/<lon>&place=<name>&cap=<fileId>&from=YYYY-MM-DD&to=YYYY-MM-DD&fav=1
```

`map` follows the OSM `zoom/lat/lon` convention. All keys optional; unknown keys
ignored; malformed values dropped individually (never throw). Beyond the three
requested dimensions (viewport, place, capture) the hash also carries the date
range and favorites flag from features 3–4, so a shared link reproduces the
whole view.

**Pure module `frontend/src/urlState.ts`** (+ `.test.ts`):
`parseHash(hash: string): UrlState`, `formatHash(state: UrlState): string`
(returns `''` when empty), roundtrip-stable, precision: lat/lon 5 decimals,
zoom 2.

**Wiring.**

- MapView gains `initialView?: {longitude, latitude, zoom}` (seeds its internal
  `view` state) and `onViewChange?(v)` called from the existing `syncBounds`
  moveend path. App holds the latest view in a ref and rewrites the hash on
  change.
- `place`: set when LocationPanel `onPick` fires (App remembers the picked place
  name), cleared on the next manual map interaction is *not* attempted —
  place is a breadcrumb, `map=` always wins for camera restore. On load with
  `place=` and no `map=`, resolve the place bbox from the loaded library
  (`buildPlaces`) and `flyTo` it.
- `cap`: written while the lightbox is open (tracks prev/next), removed on
  close. On load, once features arrive, find `properties.id === cap`; open a
  solo lightbox `{files: [f], index: 0}` and `flyTo` its point (small bbox).
- `from`/`to`/`fav` mirror the App-level filter state from features 3–4.

Restore runs exactly once (a ref guard) after the first successful library load.

## 3. Date-range scrubber

**Model.** Month-resolution brush over the library's span. Pure module
`frontend/src/dateRange.ts` (+ tests), no `Date` parsing — `local_date`
(`YYYY-MM-DD`) strings compare lexicographically:

- `buildTimeline(features): {months: {key: 'YYYY-MM', count}[], ...}` — dense
  month sequence from min to max (zero-count months included so the axis is
  linear).
- `filterByDateRange(features, range: {from, to} | null)` — inclusive; a
  feature's date comes from `local_date` (fallback: `capture_ts_local` date
  part); dateless features pass when range is null, are excluded otherwise.
- Helpers mapping brush indices ↔ `{from: 'YYYY-MM-01', to: 'YYYY-MM-31'}`
  (`-31` upper bound is safe under string comparison).

**UI.** `TimelineScrubber` component: a floating bottom-center bar over the map
(same visual family as the toolbar pill), toggled by a Toolbar `Timeline`
button. Renders month bars (heights ∝ count, accent color inside the selected
range), two pointer-draggable handles, the selected span label, and Clear.
Pointer-event math delegates to the pure module. Hidden on mobile (<1024px)
initially — the brush needs horizontal room.

**Filter composition (App).** The range filter is applied *before* both MapView
and the list, unlike the media chips (which stay panel-local):

```
visible = filterByDateRange(applyFavorites(features, ...), range)
<MapView features={visible}>   panelFiles = featuresInBounds(visible, bounds)
```

An active range shows in the under-toolbar chip slot ("Jan 2025 – Jun 2025 ·
Clear", `.track-chip` styling family).

## 4. Video favorites

**Persistence — keyed by content hash.** `files.id` dies on undo/re-import;
`sha256` is the stable identity organize itself dedupes on. New table:

```sql
CREATE TABLE IF NOT EXISTS favorites (
    sha256 TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**API.**

- `POST /api/favorite` (guarded), body `{"file_id": int, "favorite": bool}` →
  resolves the file's sha256 (404 unknown id), inserts (`INSERT OR IGNORE`) or
  deletes the favorites row. Returns `{"file_id", "favorite"}`. Idempotent.
  Backend accepts any media type; the *UI* only offers the toggle on videos per
  the feature request.
- `/api/library`: each feature gains `is_favorite: bool`
  (`EXISTS(favorites where sha256 matches)` via a join). ETag: schema token
  bumps `lib3-` → `lib4-`, and the aggregate gains favorite count + sum of
  favorited organized file ids so an in-place toggle invalidates cached
  payloads (mirrors the stitch-status code-sum rationale).

**Frontend.**

- `FeatureProps.is_favorite?: boolean`; `api.ts` `setFavorite(fetch, id, fav)`.
- Lightbox: a heart toggle (♥/♡) next to the flight-path/frames action buttons,
  shown when `media_type === 'video'` and an `onToggleFavorite` callback is
  provided (admin-gated, like retag). Reflects effective state.
- App keeps `favOverrides: Map<number, boolean>` for optimistic display (the
  lightbox `files` array is a snapshot; a server reload alone wouldn't update
  it). Pure helpers in `frontend/src/favorites.ts` (+ tests):
  `effectiveFavorite(props, overrides)`, `filterFavorites(features, overrides)`.
  After a successful POST the app records the override and calls `reload()`;
  overrides reset when fresh features commit.
- Favorites view: Toolbar `♥ Favorites` toggle → App-level `favoritesOnly`
  boolean filtering `features` before map + list (composes with the date
  range); active state shows a chip ("Showing favorites · Clear") and is
  encoded as `fav=1` in the URL hash.

## Testing

- Backend (pytest): `duplicates` recording/pruning in `test_organize.py`
  (relocate off records, relocate on doesn't leave rows, import prunes stale
  row); new `test_duplicates.py` for list/dismiss (moves group, suffixes
  collisions, logs, missing-source dismiss); `test_api.py` — GET /api/duplicates
  shape, dismiss happy path + 409-while-busy + guard (add both POST routes to
  `_MUTATING_ROUTES`), POST /api/favorite lifecycle + 404, library
  `is_favorite` + ETag flip on toggle, `lib4-` token; `test_db.py` schema v5.
- Frontend (vitest, pure modules only, matching repo convention):
  `urlState.test.ts`, `dateRange.test.ts`, `favorites.test.ts`; api.test.ts
  additions for `setFavorite`/`fetchDuplicates`/`dismissDuplicates`.
- End-to-end: seed a temp library, run the API, drive the UI (vite build +
  manual smoke via browser) for all four features.

## Out of scope

Deleting duplicate files outright (dismiss only relocates), multi-user
favorites, day-resolution brushing, back/forward navigation between hash
states (replaceState only), reacting to hash edits after initial load.
