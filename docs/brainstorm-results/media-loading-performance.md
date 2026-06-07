# Media & Page Loading Performance — Results

## Topic
The `geosorter serve` map UI is slow to load and some media (panoramas, HEVC video)
effectively never load. We brainstormed how to cut loading times and prepare the
system for an **imminent mass upload of ~5,000–20,000 mixed-media files** (lots of
photos AND HEVC video) onto a library that lives on a **LAN SMB share**
(`Z:\drones\media` → `\\192.168.1.69\projects`).

The investigation (read-only measurement of the real library) found the dominant
cost is **network I/O over the SMB share**, amplified by **lazy, synchronous,
on-request generation** of every derived asset with the cache co-located on the
same share. The current library is only 7 files, so all slowness is per-item over
the network — but the design must hold at thousands.

## Key Features
- **Tiered, relocatable derived cache** (`cache_dir`): thumbnails/posters/previews on
  local SSD; HEVC proxies + panorama stitches on the SSD-SMB share, on-demand, with a
  local size-cap + atime-sweep eviction backstop.
- **Faster, resilient bulk `organize`** for the upload: hash-while-copying (one SMB
  pass instead of 3–4), per-file SMB retry, real disk-space guarding, chunked/restartable
  ExifTool extraction, and a true progress/ETA.
- **Scalable library feed**: GZip + ETag/conditional-GET on `/api/library` so a
  20k-feature payload (8–12 MB) compresses to ~1–2 MB and unchanged reloads return 304.
- **Fast thumbnails**: Pillow `draft()` DCT-downscale + a concurrency-capped, viewport-
  prioritized post-organize warm pass.
- **Correct caching at scale**: cache-key fix (no cross-capture collisions on the mapped
  SMB drive), SMB-mtime-safe freshness, `_safe_cache_path` guard.
- **Non-blocking HEVC video**: transcode as a tracked background job instead of blocking
  the request thread.
- **Browse at scale**: virtualized file-list panel.
- **Instant panoramas**: a raw-tile collage placeholder shown immediately; full Hugin
  stitch stays user-triggered (+ smaller 4000×2000 canvas, tuned steps).

## Justification
- **Evidence (measurement):** library + cache on a LAN SMB share; 72 MP photos, 4K H.265
  video (3–5.5 min), panoramas of ~23 tiles. Every byte — sources *and* cache hits —
  crosses the LAN. Hugin is installed, so the 5–10 min stitch really runs.
- **Architect + Critic + Risk converged** that the bulk-upload bottleneck is **redundant
  SMB reads, not CPU**: each file is read over SMB 3–4× (dedup hash + copy-verify source
  hash + the copy + the destination `.partial` re-hash). Eliminating the redundant reads is
  the single highest-leverage, low-risk throughput win.
- **Risk** identified concrete upload-blockers that would make a multi-hour 20k-file run
  fail or corrupt: a single SMB disconnect aborts the whole batch; the 200 MB disk margin
  is far too small for multi-GB HEVC with no mid-run free-space check; the ExifTool pass has
  no checkpoint (a daemon crash at file 15k loses all extraction); a `Path.resolve()`-based
  cache key can collide common DJI basenames and serve the *wrong* thumbnail.
- **Parallelism question answered:** keep `ThreadPoolExecutor`, single-process uvicorn.
  NOT `ProcessPoolExecutor` (all heavy work — ffmpeg/Hugin — is already a subprocess) and
  NOT multi-worker uvicorn (each process would get its own in-memory `JobManager`, breaking
  job-status routing). SMB bandwidth — not CPU — is the binding constraint, so parallelizing
  the crash-safe move engine adds risk for no throughput gain.
- **User priorities:** instant thumbnails on repeat visits, video that starts quickly,
  panoramas that show *something* fast — all served by the above without background jobs
  that saturate the LAN while the user is browsing or uploading.

## Scope Definition
**In scope**
- A new tiered `cache_dir`/`proxy_cache_dir` with eviction + the cache-correctness fixes.
- `organize` throughput (hash-while-copying) + resilience (retry, disk guarding, ExifTool
  chunking) + progress/ETA + a "worker busy" 409 — sized for a one-shot 5–20k upload.
- `/api/library` GZip + ETag/conditional-GET (+ supporting index).
- Pillow `draft()`, a concurrency-capped warm pass, derived-asset correctness/safety
  (`_safe_cache_path`, `_is_fresh` SMB hardening, `_run_ffmpeg` timeout, `_lookup_codec` fix).
- HEVC video as a background transcode job (proxies on the SSD-SMB tier, on-demand).
- File-list virtualization.
- Panorama instant raw-tile collage + smaller stitch canvas + Hugin step tuning + an
  optional, interruptible "Stitch all" button.
- Four de-risking spikes (below).

**Out of scope / explicitly deferred**
- Relocating the library off the SMB share (hard constraint: it stays).
- `ProcessPoolExecutor` / multi-worker uvicorn / shared job state (no CPU bottleneck;
  breaks the job model). Revisit only if profiling proves a CPU wall.
- Server-side GeoJSON clustering / bbox-or-zoom pagination (premature below ~100k features;
  supercluster handles 20k client-side). Spike-gated.
- **Auto-stitch-all panoramas in the background** (CPU/LAN footgun; stitch stays
  user-triggered).
- **202+enqueue for images** (sync + semaphore + virtualization + the existing
  `LoadingImage` retry bound the cold-miss burst; jobs reserved for HEVC video).
- A low-res Hugin "preview stitch" (cpfind dominates regardless of output resolution).
- A full per-write LRU ledger (a periodic atime-sweep + size cap suffices).

## De-risking Spikes (run before/with the first phase)
1. **SMB copy throughput** inbox→library: measure real MB/s — determines whether the
   20k-file upload is ~7 h or ~28 h and whether the throughput design is sufficient. **CRITICAL.**
2. **SHA-256-over-SMB cost** on a representative large file — gates the exact hash-while-copying design.
3. **GeoJSON payload size** at 5k/10k/20k rows (with GZip) — confirms conditional-GET is
   enough and bbox/clustering is unneeded.
4. **supercluster** load/cluster time for 20k points in-browser — confirms client-side
   clustering holds.

## Feature Details

### Tiered relocatable cache (`cache_dir` / `proxy_cache_dir`)
New optional `Config` fields (mirroring the `hugin_bin_dir` `_opt_path` pattern). Resolve a
per-kind cache root: thumbs/posters/previews under `cache_dir` (default a local
`platformdirs.user_cache_dir`); proxies/stitches under `proxy_cache_dir` (default the
SSD-SMB share). `_cache_path` takes an explicit `cache_root` per kind; `_atomic_write`'s
temp-in-same-dir keeps `os.replace` atomic per volume. Eviction = a periodic atime-sweep +
`cache_max_gb` size cap on the **local** tier only, deferring on Windows `PermissionError`
(open file). Proxies/stitches: manual purge verb, no auto-evict.

### Cache correctness at scale
- **Cache-key fix (SEC-001):** build the key by string-stripping the unresolved
  `cfg.library_root` prefix (the approach `_relpath` already uses), not
  `source.resolve().relative_to(...)` — which on the mapped SMB drive falls back to the bare
  filename and collides `DJI_0001.JPG` across days (serving the wrong thumbnail).
- **`_is_fresh` SMB hardening:** after `_atomic_write`, `os.utime` the cache file to the
  source mtime (+ a small offset) so SMB's 2-second mtime granularity can't yield a false
  "stale" or a false "fresh"; tolerate `OSError` from a disconnected share.
- **`_safe_cache_path` guard:** validate every served derived path is under its cache root
  (now that the cache can live outside `library_root`); validate `cache_dir`/`proxy_cache_dir`
  at config load (absolute; not inside `library_root`/`inbox`).

### Bulk `organize` throughput
- **Hash-while-copying:** accumulate the SHA-256 inside `_copy_file`'s existing chunked
  stream and pass the pre-computed source hash into `copy_and_verify`, eliminating the
  separate source re-hash and the destination `.partial` re-hash. Reuse the `organize.py`
  dedup hash. Net: ~1 SMB pass per file instead of 3–4. Crash-safety unchanged (the hash is
  still verified before `os.replace`). The move engine stays **sequential** (SMB bandwidth-bound).

### Bulk `organize` resilience
- **Per-file SMB retry+backoff** in `copy_and_verify` (transient `OSError`), keeping the
  group-atomic abort contract intact.
- **Disk guarding:** raise the disk margin to ~5 GB (configurable), add a periodic mid-run
  free-space recheck, and clean stale `.partial` files on resume.
- **ExifTool at scale:** chunk pass-1 extraction (~500 groups/daemon), restart-on-failure,
  quarantine-after-N — so a daemon crash loses at most one chunk, not the whole pass.
- **Progress/ETA:** add `total_groups` + a bytes-based ETA to `JobState` (prescan already
  sums sizes). Return **409 "worker busy"** for undo/retag/rescan submitted during a
  multi-hour organize instead of silently queuing.

### Scalable library feed
Add `GZipMiddleware` (8–12 MB GeoJSON → ~1–2 MB) and an `ETag`/`Last-Modified` +
`If-None-Match` 304 path on `/api/library` keyed on `MAX(id)`+`COUNT(*)` of organized
geolocated rows; add `idx_files_status_latlon`. Client (`useLibrary.ts`) sends the prior
ETag and keeps stale features visible while revalidating, so a reload after organize is a
304 rather than a re-parse.

### Fast thumbnails + warm pass
Add `img.draft("RGB", ...)` (guarded to `.jpg`) in `_resize_jpeg` (≈4–8× faster decode of
large DJI JPEGs, fewer SMB bytes read). Cap concurrent Pillow generation with a semaphore /
small dedicated pool. After an organize batch, a throttled, viewport-prioritizable warm pass
pre-generates thumbnails + posters (local tier) so the first browse is warm; it yields to
foreground requests and never touches the destructive pool. `_lookup_codec` keyed by id
(drop the O(N) scan); `_run_ffmpeg` gets a timeout.

### Non-blocking HEVC video
Move proxy transcoding off the request thread into a tracked background job (mirroring the
existing stitch-job pattern), proxies cached on the SSD-SMB tier and generated on-demand
(never bulk pre-warmed). The player shows progress and loads the proxy when ready. Optional
in-flight coalescing so concurrent opens share one transcode.

### Browse at scale
Virtualize the `FileListPanel` grid so only viewport-visible thumbnails mount/request,
bounding the cold-miss burst regardless of cluster size.

### Instant panoramas
Show a cheap raw-tile collage immediately (Pillow, no Hugin) on lightbox open. The full 360
Hugin stitch stays user-triggered (existing button); shrink the canvas to 4000×2000 and make
`--celeste`/`-l` opt-out to cut stitch time. Add an optional, interruptible "Stitch all
panoramas" button for users who want a batch — never automatic.

## Reusable Code
- `src/geosorter/derived.py:69`–`113` — `_cache_path` / `_atomic_write` / `_is_fresh`
  absorb tiered `cache_root` with a single new argument; `_atomic_write` (temp-in-same-dir)
  is volume-agnostic and unchanged.
- `src/geosorter/derived.py:116`–`129` — `_resize_jpeg` is the `draft()` insertion point (3-line change).
- `src/geosorter/move_engine.py` `_copy_file` (chunked stream) + `copy_and_verify:73` +
  `sha256_file:35` (`on_bytes` hook) — the hash-while-copying + progress insertion points;
  `is_already_moved:64` + `UNIQUE(source_path, source_sha256)` give resumability for free.
- `src/geosorter/jobs.py:346`–`442` — the stitch-pool `submit/_run/status/on_step` pattern
  is the 1:1 template for the warm pass and the HEVC proxy job; `byte_progress` (jobs.py:193)
  feeds ETA.
- `src/geosorter/config.py:136` — `_opt_path`/`hugin_bin_dir` is the exact model for
  `cache_dir`/`proxy_cache_dir`/`cache_max_gb`; `db.py` `migrate_index_schema` is the
  additive pattern for `idx_files_status_latlon`.
- `src/geosorter/api.py:81`–`105` — `_relpath`'s string-prefix root handling is the template
  for the cache-key fix.
- `frontend/src/loadingImage.ts`, `components/LoadingImage.tsx`, `api.ts`, `useLibrary.ts` —
  isolated frontend change points (cache-buster removal, ETag revalidation, virtualization).

## Deprecated / Dead Code
- `src/geosorter/derived.py:33,81` — `CACHE_DIRNAME`-under-`library_root` join: **modify**
  (decouple from `library_root`; per-kind roots).
- `src/geosorter/derived.py:76`–`81` — `_cache_path` `Path.resolve().relative_to(...)`:
  **modify** (cross-capture collision risk, SEC-001).
- `src/geosorter/derived.py:90`–`92` — `_run_ffmpeg` (no timeout): **modify** (add timeout +
  delete partial).
- `src/geosorter/api.py:323`–`341` — inline synchronous `derived.*` route calls: **modify**
  (video → background job; semaphore for images).
- `src/geosorter/api.py:343`–`353` — `_lookup_codec` O(N) scan: **modify** (key by id).
- `src/geosorter/move_engine.py:126`,`152`–`153` + `organize.py:475` — redundant source
  re-hash + dest `.partial` re-hash + dedup re-hash: **modify** (hash-while-copying).
- `src/geosorter/organize.py` `_disk_preflight` (200 MB margin, once): **modify** (5 GB +
  periodic recheck).
- `frontend/src/loadingImage.ts:6` + `components/LoadingImage.tsx:43` — cache-buster `?_r=N`
  retry: **partially obsolete** (remove the buster; keep same-URL retry).
- `tests/test_derived.py:31,92,189,238,271` — assertions on `.geosorter-cache` under
  `library_root`: **modify** (tiered cache roots).

## Implementation Plan
Granularity: **Option B — Balanced (7 tasks)**. The first four are the **pre-upload
critical path** (they make the 5–20k-file import fast, safe, and correct); the last three
are browse-experience work that can land after the library is populated.

### Pre-upload critical path (must merge before the mass upload)

| # | Task | Scope | Depends on | Effort / Impact |
|---|------|-------|-----------|-----------------|
| 1 | `r-perf-spikes` | The 4 de-risking spikes: **SMB copy throughput** inbox→library (CRITICAL — sets the upload-time expectation), SHA-256-over-SMB cost, GeoJSON payload size at 5k/10k/20k (gzipped), supercluster load/cluster at 20k. A research task producing measured numbers that gate tasks 2 and 5. | — | S / H |
| 2 | `m-organize-throughput` | Hash-while-copying in `_copy_file` + thread a pre-computed source hash into `copy_and_verify`; reuse the `organize.py` dedup hash. Collapses 3–4 SMB reads/file to ~1. Move engine stays sequential. | 1 (SHA-256 spike) | M / H |
| 3 | `m-organize-resilience` | Per-file SMB retry+backoff in `copy_and_verify`; disk margin → ~5 GB (config) + periodic free-space recheck + stale-`.partial` cleanup on resume; chunked ExifTool pass-1 (~500/daemon) + restart-on-failure + quarantine-after-N; `total_groups` + bytes-ETA in `JobState`; 409 "worker busy" for undo/retag/rescan during organize. | — (parallel with 2) | M / H |
| 4 | `m-cache-tiering-safety` | `cache_dir`/`proxy_cache_dir`/`cache_max_gb` config + per-kind cache roots (thumbs/posters/previews local; proxies/stitches on SSD-SMB); cache-key string-prefix fix (SEC-001); `_safe_cache_path` guard + config-load validation; `_is_fresh` SMB-mtime hardening (`os.utime` + tolerate `OSError`); `_run_ffmpeg` timeout. | — | M / H |

### Post-upload browse experience

| # | Task | Scope | Depends on | Effort / Impact |
|---|------|-------|-----------|-----------------|
| 5 | `m-library-feed-scale` | `GZipMiddleware` + ETag/`If-None-Match` 304 on `/api/library` (keyed on `MAX(id)`+`COUNT(*)`) + `idx_files_status_latlon`; client (`useLibrary.ts`) ETag revalidation + stale-while-revalidate; Pillow `draft()` for `.jpg` thumbs; `_lookup_codec` keyed by id. | 1 (GeoJSON spike) | M / H |
| 6 | `m-derived-at-scale` | Concurrency semaphore on derived generation; throttled, viewport-prioritizable post-organize warm pass (thumbs/posters, local tier, yields to foreground); atime-sweep + size-cap eviction (local tier, `PermissionError`-deferring); HEVC proxy as a tracked background job (proxies on SSD-SMB, on-demand) + in-flight coalescing. | 4 (cache tiering) | L / H |
| 7 | `m-frontend-pano-ux` | Virtualize the `FileListPanel` grid; instant raw-tile collage placeholder on lightbox open; full Hugin stitch stays user-triggered + smaller 4000×2000 canvas + `--celeste`/`-l` opt-out; optional interruptible "Stitch all panoramas" button. | 4, 5 | M / M |

**Sequencing:** Run task 1 first (or alongside 2–4). Tasks 2, 3, 4 are independent and can
proceed in parallel after the spikes. Task 6 depends on the cache tiering from task 4; task 7
depends on tasks 4 and 5. Backend vs frontend split: task 5 has a small frontend slice
(`useLibrary` revalidation); task 7 is mostly frontend.

**Explicitly NOT tasks** (deferred/cut per Decisions): library relocation off SMB;
ProcessPool/multi-worker; server-side GeoJSON clustering/bbox; auto-stitch-all; 202-for-images;
low-res Hugin preview; per-write LRU ledger.
