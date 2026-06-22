# File-list date grouping, media-type filters, and unstitched-panorama panel

**Date:** 2026-06-21
**Scope:** Frontend SPA (`frontend/`) only. No backend/API/schema changes — all three
features read fields already present in the `/api/library` GeoJSON feed.

## Problem

The right-hand file-list panel lists every capture in the current map viewport as a flat,
ungrouped thumbnail grid. The user wants:

1. A **default time-based organization** with headings — e.g. a heading `April 2024`
   followed by that month's media — and a way to switch the grouping granularity between
   **day**, **month**, and **year**.
2. A way to **filter by media type**: video, photo, panorama, hyperlapse.
3. A way to **see exactly which panorama sets are waiting to be stitched** (today only the
   library-wide count is visible, on the toolbar's "Stitch all panoramas (N)" button).

## Decisions (from brainstorming Q&A)

- **Filter scope:** viewport-driven. Grouping and media filters apply to the captures
  currently in the map view (today's panel behavior), not the whole library.
- **Date grouping:** defaults to **Month**; a **Day / Month / Year** toggle switches
  granularity. Groups are ordered **newest-first**.
- **Media filter:** four **mutually-exclusive** buckets, **multi-select**, all enabled by
  default. Each capture is classified into exactly one bucket.
- **Unstitched panoramas:** a **dedicated, library-wide** toolbar-opened panel (mirrors the
  existing `LocationPanel` / `QuarantinePanel`), independent of map position.

## Design (as implemented)

### Part 1 — Date grouping

`frontend/src/dateGroups.ts` (+ `dateGroups.test.ts`), reusing the by-regex, timezone-stable
parse from `captionInfo.ts` (never `new Date()`):

- `type Granularity = 'day' | 'month' | 'year'`
- `parseParts(props)` → `{year, month, day}` from `capture_ts_local`, falling back to
  `local_date`; `null` when undated.
- `groupFeatures(files, gran)` → ordered `{ key, label, files }[]`, **newest-first** by key.
  Labels: day `"April 12, 2024"`, month `"April 2024"`, year `"2024"`. Undated captures
  collect into a trailing `"Unknown date"` group. Files keep incoming order within a group.
- `buildRowModel(groups, columns)` → flat `RowItem[]`:
  `{kind:'header', key, label}` | `{kind:'thumbs', key, files}`. Each thumb row holds ≤
  `columns` files and never spans two groups.

`FileListPanel.tsx`: a Day/Month/Year segmented control (local state, default `'month'`); the
virtualizer now windows the **row model** — `estimateSize` returns `HEADER_PX` for header rows
and `estRow` for thumb rows, `measureElement` corrects both. Only visible thumb rows mount a
`LoadingImage` (bounded-request property preserved). Headers scroll normally (not sticky).

### Part 2 — Media-type filter

`frontend/src/mediaFilter.ts` (+ `mediaFilter.test.ts`):

- `type MediaCategory = 'photo' | 'video' | 'panorama' | 'hyperlapse'`
- `categoryOf(props)` with precedence **panorama → hyperlapse → video → photo**.
- `filterByCategories(files, enabled: Set<MediaCategory>)`.
- `MEDIA_CATEGORIES` export seeds the panel's all-on default.

`FileListPanel.tsx`: four toggle chips in a `.panel-controls` row (local `Set` state, all on by
default). Filter applies **before** grouping. Empty filtered result shows `.panel-empty`
("No captures match the filter" when files exist but are filtered out, else "No captures in view").

### Part 3 — Unstitched-panorama panel (library-wide)

`frontend/src/components/StitchPanel.tsx` (mirrors `LocationPanel`): a toolbar-opened list of
every panorama with tiles whose `stitch_status !== 'ok'`. Each row: thumbnail (opens the
lightbox via `onView`), filename + `captionInfo` place/date, a live status line from
`stitchByFile`, and an admin-gated per-item **Stitch** button (`onStartStitch`, disabled while
that file's stitch is in flight).

`App.tsx`: `panoramaTargetFeatures` (memoized full features; `panoramaTargets` ids derived from
it for the toolbar), `showStitch` state, `StitchPanel` wired to the App-level `useStitch`
(`stitchByFile` / `startStitch`, the latter only when `isAdmin`) and a lightbox opener over the
panorama list.

`Toolbar.tsx`: an admin-only **"Unstitched panoramas (N)"** button (`onOpenStitch` prop, shown
when `stitchTargets.length > 0`). The existing "Stitch all panoramas (N)" button is unchanged.

### Cross-cutting — lightbox/re-tag order correctness

Grouping + filtering changes the **displayed** order relative to raw `panelFiles`, so the panel
passes the real ordered/filtered data: `onOpen(files, index)` (the lightbox snapshots the
displayed order) and `onRetag(file)` (re-tag targets the clicked feature directly). `App.tsx`
updated accordingly.

## Testing

TDD on the pure modules: `dateGroups` (13 tests) and `mediaFilter` (7 tests). Full suite: 167
passing. `npm --prefix frontend run build` (tsc + vite) succeeds. Components are wired thinly
over the tested pure logic (project convention: pure logic Vitest-tested, components render).

## Out of scope (YAGNI)

- Sticky group headers.
- Persisting filter/granularity selections across reloads.
- Any backend, API, or schema change.
- Applying grouping/filters to the whole library (chose viewport scope).
