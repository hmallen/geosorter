"""FastAPI backend for the map viewer (B6).

Exposes the organized library over HTTP for the B7 frontend:

* ``GET  /api/library`` — the whole library as a GeoJSON ``FeatureCollection``
  (one ``Point`` per organized, geolocated file), loaded once.
* ``GET  /api/inbox`` — ``{files, captures}`` counts of what is waiting in the
  inbox for the next ``organize`` run (B8; see :mod:`geosorter.inbox`).
* ``POST /api/organize`` / ``GET /api/organize/status/{id}`` /
  ``POST /api/organize/cancel/{id}`` — run the Phase 0 pipeline as a cancellable
  background job (see :mod:`geosorter.jobs`).
* ``POST /api/undo`` / ``GET /api/undo/status/{id}`` / ``POST /api/undo/cancel/{id}``
  — reverse the most recent organize batch as a cancellable background job (B8;
  see :mod:`geosorter.undo`). Shares the single-worker pool with organize.
* ``POST /api/retag`` (``{file_id, lat, lon}``) / ``GET /api/retag/status/{id}`` —
  re-file an organized capture to a map-clicked location as a background job (B8;
  see :mod:`geosorter.retag`). Shares the single-worker pool with organize/undo.
* ``POST /api/rescan`` / ``GET /api/rescan/status/{id}`` — reconcile the index with
  on-disk state, pruning rows for files no longer in the library, as a background
  job (see :mod:`geosorter.rescan`). Shares the single-worker pool with
  organize/undo/retag; no cancel route (it is a fast index-only pass).
* ``POST /api/assign-location`` (``{file_ids, lat, lon}``) / ``GET
  /api/assign-location/status/{id}`` — bulk-file no-GPS captures to a picked
  location (see :mod:`geosorter.setloc`). Shares the single destructive worker.
* ``GET  /api/quarantine`` — the no-GPS (quarantined) captures list.
* ``POST /api/repair/scan`` / ``GET /api/repair/scan/status/{id}`` — ffprobe every
  quarantined capture for corruption (0-byte, missing ``moov`` atom) on the repair
  worker pool. ``GET /api/repair/untrunc`` reports untrunc availability;
  ``GET /api/repair/references/{file_id}`` ranks healthy reference clips;
  ``POST /api/repair/run`` / ``GET /api/repair/status/{id}`` rebuild a truncated
  clip with untrunc into ``_repair/fixed/`` for preview; the synchronous
  ``POST /api/repair/accept`` / ``discard`` / ``delete`` then swap it into the
  library, drop the attempt, or delete a (re-verified) broken file + its index
  rows (see :mod:`geosorter.repair`).
* ``GET  /api/duplicates`` / ``POST /api/duplicates/dismiss`` — the pending
  re-import backlog (``relocate_duplicates = false``) and its synchronous drain
  (see :mod:`geosorter.duplicates`).
* ``POST /api/favorite`` — toggle a content-hash favorite; ``/api/library``
  features carry the resulting ``is_favorite`` flag.
* ``GET  /api/inbox/list`` — the inbox as a selectable capture-group tree.
* ``GET  /api/place-search?q=`` — forward GeoNames search for assign-by-name.
* ``POST /api/stitch/{id}`` / ``GET /api/stitch/status/{job}`` — panorama hero
  stitch on its own worker pool; ``GET /api/stitch/{id}`` serves the hero and
  ``GET /api/collage/{id}`` the instant raw-tile collage.
* ``GET  /api/frames/{id}`` — a hyperlapse/panorama's source-frame list.
* ``GET  /api/auth`` / ``POST /api/login`` / ``POST /api/logout`` — admin-auth
  probe + bearer-token session management (throttled on failed logins).
* ``GET  /api/media/{relpath}`` — original file, range-capable (video seek),
  path-traversal-guarded (cache internals excluded).
* ``GET  /api/thumb/{relpath}`` / ``GET /api/poster/{relpath}`` /
  ``GET /api/preview/{relpath}`` — lazily generated, cached derived images
  (see :mod:`geosorter.derived`).
* ``GET  /api/video/{relpath}`` — a browser-playable video: H.264 originals served
  directly, HEVC served as a cached H.264 proxy.

The app binds to ``127.0.0.1`` (the ``serve`` CLI verb owns the socket); the
GeoJSON embeds home GPS coordinates, so it never leaves the loopback interface
unless the operator explicitly opts in via ``--host``.

Admin auth (m-implement-view-only-admin-auth) is OPTIONAL: when an
``admin_password_hash`` is set in config, the mutating routes (organize / undo /
rescan / retag / assign-location / stitch) require a bearer token from
``POST /api/login`` via the ``require_admin`` dependency, and the map UI is
view-only until login. All reads stay public. With no password configured the
guard is a no-op and the whole app is open, as before.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from . import (
    auth, config, db, derived, duplicates, geocoder, inbox, pathing, repair,
    srt_parser,
)
from .jobs import JobManager, WorkerBusy

logger = logging.getLogger("geosorter.api")

# Ceiling on /api/track points: an SRT has one fix per frame (30-60/s — tens of
# thousands for a long clip); a map polyline reads the same at a few hundred.
_TRACK_MAX_POINTS = 500


def _downsample_track[T](items: list[T]) -> list[T]:
    """Evenly cap map/timeline data while preserving both endpoints."""
    if len(items) <= _TRACK_MAX_POINTS:
        return items
    step = len(items) / _TRACK_MAX_POINTS
    return [items[int(i * step)] for i in range(_TRACK_MAX_POINTS - 1)] + [items[-1]]


def _safe_cache_path(path: Path, *roots: Path) -> Path:
    """Assert a served derived file lives under one of its cache ``roots``, else 403.

    Once the cache can live outside ``library_root`` (tiered), the ``_safe_path``
    traversal guard (which checks under ``library_root``) no longer covers a served
    derived ``FileResponse``. This is the defense-in-depth equivalent for the cache
    tiers: both sides are ``.resolve()``d (mapped-drive-safe, like ``_safe_path``), and
    the video route passes both ``proxy_cache_dir`` AND ``library_root`` so an h264
    passthrough (which returns the source itself) is allowed alongside a real proxy.
    """
    resolved = path.resolve()
    if not any(resolved.is_relative_to(Path(r).resolve()) for r in roots):
        raise HTTPException(status_code=403, detail="derived path outside cache")
    return path


def _gen_or_http(produce: Callable[[], Path]) -> Path:
    """Run a derived-asset generation, mapping its failure to a clean HTTP status.

    Corrupt/truncated media would otherwise surface a ``derived`` exception as an
    unhandled 500 with a full ASGI traceback (and the frontend retries each broken
    ``<img>``, multiplying the ffmpeg runs + spam). Instead:
    a :class:`derived.SourceUnrenderable` (the source can never be decoded) -> a
    permanent **422**; any other generation error (a transient I/O blip) -> a retryable
    **503**. Both log ONE concise ``geosorter.api`` warning, never a traceback. Compute
    ``_safe_path`` OUTSIDE the ``produce`` thunk — its own 403/404 must not be remapped.
    For the image kinds, ``derived`` returns a cached placeholder rather than raising
    :class:`~derived.SourceUnrenderable`, so they only ever reach the 503 branch.
    """
    try:
        return produce()
    except derived.SourceUnrenderable as exc:
        logger.warning("unrenderable source media: %s", exc)
        raise HTTPException(status_code=422, detail="source media is unreadable")
    except (RuntimeError, OSError) as exc:
        logger.warning("derived asset generation failed: %s", exc)
        raise HTTPException(status_code=503, detail="derived asset generation failed")


def _submit_or_409(submit) -> dict:
    """Run a destructive-job submission, mapping :class:`WorkerBusy` to HTTP 409.

    undo/retag/rescan share the single destructive worker with ``organize``; when one
    is in flight, submitting another answers ``409`` with the blocking job id rather
    than silently queueing behind a multi-hour run.
    """
    try:
        return {"job_id": submit()}
    except WorkerBusy as busy:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "a destructive job is already running",
                "blocking_job_id": busy.blocking_job_id,
            },
        )


class OrganizeRequest(BaseModel):
    """Optional body of ``POST /api/organize``: import a chosen subset of the inbox.

    ``primaries`` is a list of inbox-relative POSIX primary paths (the ``id`` field
    from ``GET /api/inbox/list``). ``None`` (or a missing body) imports the whole
    inbox — the map UI sends ``None`` when "Select All" is on, preserving today's
    full-import behavior (including MISC-catalog archiving).
    """

    primaries: list[str] | None = None


class RetagRequest(BaseModel):
    """Body of ``POST /api/retag``: re-file ``file_id`` to a clicked coordinate.

    ``lat``/``lon`` are constrained to valid WGS84 ranges so an out-of-range click
    is rejected with a clean 422 rather than failing deep in the job.
    """

    file_id: int
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class AssignRequest(BaseModel):
    """Body of ``POST /api/assign-location``: assign one coordinate to N no-GPS captures.

    ``file_ids`` are quarantined ``files`` row ids; ``lat``/``lon`` are WGS84-bounded
    so an out-of-range pick is rejected with a clean 422 rather than failing deep in
    the job. Each capture keeps its own date, so one coordinate fans out to one date
    folder per capture.
    """

    file_ids: list[int]
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class StitchRequest(BaseModel):
    """Optional body of ``POST /api/stitch/{file_id}`` (manual re-stitch).

    A bare POST (no body) keeps the original first-time behaviour. ``force`` re-runs
    the Hugin pipeline cold even when a fresh hero is cached; ``projection`` overrides
    the auto-detected projection — constrained to the three real Hugin projections so
    an unknown value is rejected with a clean 422.
    """

    force: bool = False
    projection: Literal["equirectangular", "cylindrical", "rectilinear"] | None = None


class RepairRunRequest(BaseModel):
    """Body of ``POST /api/repair/run``: rebuild ``file_id`` using ``reference_id``.

    Both are ``files`` row ids: the broken capture and the healthy clip untrunc
    uses as its structural example (picked from ``GET /api/repair/references``).
    """

    file_id: int
    reference_id: int


class RepairFileRequest(BaseModel):
    """Body of the synchronous repair steps (accept / discard / delete): one row id."""

    file_id: int


class DismissDuplicatesRequest(BaseModel):
    """Body of ``POST /api/duplicates/dismiss``: pending-duplicate row ids to drain.

    Non-empty by construction — an empty list is a client bug, rejected with a
    clean 422 rather than answering a vacuous ``{"dismissed": 0}``.
    """

    ids: list[int] = Field(min_length=1)


class FavoriteRequest(BaseModel):
    """Body of ``POST /api/favorite``: set/clear the favorite flag for a file."""

    file_id: int
    favorite: bool


class LoginRequest(BaseModel):
    """Body of ``POST /api/login``: the single shared admin password."""

    password: str


def _bearer(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header, else ``None``."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


# Shared long-path prefix handling (single UNC-aware implementation).
_strip = pathing.strip_long_prefix


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 7232 ``If-None-Match`` membership test (handles a list and ``*``)."""
    candidates = [tok.strip() for tok in if_none_match.split(",")]
    return "*" in candidates or etag in candidates


def _http_date(sqlite_ts: str | None) -> str | None:
    """Format a SQLite ``datetime('now')`` (UTC, ``YYYY-MM-DD HH:MM:SS``) as an HTTP date.

    Returns ``None`` if the value is missing/unparseable so ``Last-Modified`` is
    simply omitted rather than wrong.
    """
    try:
        dt = datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return format_datetime(dt, usegmt=True)


def _relpath(dest_path: str, *roots: Path) -> str:
    """Library-relative POSIX path used in media URLs.

    Tries each candidate root in order and returns the path relative to the first
    one that contains ``dest_path``. Callers pass the **raw** ``cfg.library_root``
    first (it matches the stored ``dest_path`` drive form) and the ``.resolve()``d
    root as a backstop — on a mapped network drive ``.resolve()`` rewrites e.g.
    ``Z:\\...`` to a ``\\\\server\\share\\...`` UNC path that no longer matches the
    stored ``Z:\\...`` paths, which would otherwise silently degrade every media URL
    to a bare filename (404). If no root matches, log a warning and fall back to the
    bare filename so the failure is visible rather than silent.
    """
    stripped = Path(_strip(dest_path))
    for root in roots:
        try:
            return stripped.relative_to(root).as_posix()
        except ValueError:
            continue
    logger.warning(
        "dest_path %r is not under any known library root %s; serving by bare "
        "filename (media URLs may 404)",
        dest_path,
        [str(r) for r in roots],
    )
    return stripped.name


def create_app(cfg, *, spa_dir: Path | str | None = None, job_manager=None) -> FastAPI:
    """Build the FastAPI app bound to one :class:`~geosorter.config.Config`.

    ``job_manager`` is injectable for tests (e.g. a :class:`~geosorter.jobs.JobManager`
    with a fake ``stitch_fn`` so a stitch job completes without invoking real Hugin);
    it defaults to a fresh manager bound to ``cfg``.
    """
    library_root = Path(cfg.library_root).resolve()
    # Raw (unresolved) form for media-URL relpaths: stored dest_paths are built from
    # the unresolved cfg.library_root, so on a mapped drive (Z: -> UNC) the resolved
    # root above no longer matches them. _relpath tries url_root first, library_root
    # as a backstop. The traversal guard / derived cache keep using library_root.
    url_root = Path(cfg.library_root)
    # Per-kind cache tiers (m-cache-tiering-safety): thumbs/posters/previews on the
    # local SSD cache_dir (off the LAN); proxies/stitch on proxy_cache_dir (default
    # library_root). cfg.cache_dir is filled by config.load; a directly-constructed
    # Config (tests) may leave it None -> the local platformdirs default.
    cache_dir = Path(cfg.cache_dir) if cfg.cache_dir else config.default_cache_dir()
    # Proxy/stitch tier — the SAME helper jobs._run_stitch uses, so the stitch
    # generator and the serve route compute the identical cached-hero path (raw
    # library_root by default, never .resolve()d; see resolve_proxy_cache_dir).
    proxy_cache_dir = config.resolve_proxy_cache_dir(cfg)
    jobs = job_manager if job_manager is not None else JobManager(cfg)

    # Admin auth (m-implement-view-only-admin-auth): a single shared password gates
    # the mutating routes. `auth_configured` is False when no password hash is set in
    # config -> `require_admin` is a no-op and the whole app stays open (today's
    # loopback-dev behaviour, and what every existing test relies on). `tokens` holds
    # the live bearer tokens issued by /api/login (in-memory; reset on restart).
    tokens = auth.TokenStore()
    auth_configured = bool(cfg.admin_password_hash)
    # Failed-login throttle: after a burst of consecutive wrong passwords from one
    # address, /api/login answers 429 for a cooldown instead of letting an online
    # brute-force run at PBKDF2 speed.
    login_throttle = auth.LoginThrottle()

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        """FastAPI dependency: 401 unless a valid admin token is presented.

        A no-op when no admin password is configured, so an unguarded install behaves
        exactly as before. Applied to the mutating routes only — all reads stay public.
        """
        if not auth_configured:
            return
        if not tokens.valid(_bearer(authorization)):
            raise HTTPException(
                status_code=401,
                detail="admin authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Run schema creation + migration ONCE at startup on a dedicated connection,
    # not per-request: the v1->v2 ALTER TABLE migration must not race the many
    # short-lived request connections (WAL allows concurrent readers + one writer).
    _startup_conn = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        db.init_index_schema(_startup_conn)
    finally:
        _startup_conn.close()

    app = FastAPI(title="geosorter", version="0.1.0")

    def _safe_path(relpath: str) -> Path:
        """Resolve a request path under the library or raise (traversal guard)."""
        candidate = (library_root / relpath).resolve()
        if not candidate.is_relative_to(library_root):
            raise HTTPException(status_code=403, detail="path outside library")
        # Never serve a catalog DB. Archived MISC .db files live outside library_root
        # by design (B11), so this is belt-and-suspenders against any .db under it.
        if candidate.suffix.lower() == ".db":
            raise HTTPException(status_code=403, detail="forbidden type")
        # Never serve derived-cache internals (proxies, .src sidecars, stitch
        # heroes) through the original-media routes: with proxy_cache_dir ==
        # library_root (the default) the cache tree lives under the library and
        # would otherwise be reachable. NTFS is case-insensitive, so compare
        # case-folded parts.
        cache_dirname = derived.CACHE_DIRNAME.lower()
        if any(part.lower() == cache_dirname for part in candidate.parts):
            raise HTTPException(status_code=403, detail="path inside cache")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return candidate

    def _index():
        # Schema is initialised/migrated once at create_app startup, so a request
        # connection just opens and returns (no per-request init -> no WAL race).
        conn = db.connect(cfg.index_db_path, integrity_check=False)
        conn.row_factory = sqlite3.Row
        return conn

    @app.get("/api/library")
    def library(request: Request) -> Response:
        # Cheap version probe first (rows found via idx_files_status_latlon): an
        # unchanged library answers If-None-Match with 304 BEFORE building the
        # 8-12 MB feature list. MAX(id)+COUNT(*) flips on any add/prune, but the
        # in-place UPDATEs (retag.py moves lat/lon; jobs._mark_stitch_status flips
        # stitch_status / stitch_projection) leave both unchanged — so the key also
        # folds in a microdegree sum of lat/lon (any retag moves the coordinate), a
        # stitch-status code sum, AND a stitch-projection code sum (a cache-hit
        # backfill can change projection while status stays 'ok'), or a
        # retag/stitch reload would wrongly 304 and the map would keep a stale marker
        # or route a hero to the wrong viewer. A favorite toggle is likewise an
        # in-place change (a favorites row, not a files row), so the key also folds
        # in the count + id-sum of favorited files that are IN the payload — both
        # subqueries range over the exact organized+GPS predicate below, so
        # favoriting a quarantined/no-GPS file (payload byte-identical) cannot bust
        # every client's cache, while the id-sum still catches moving one favorite
        # from payload file A to payload file B (count alone misses that).
        conn = _index()
        try:
            ver = conn.execute(
                "SELECT MAX(id), COUNT(*), MAX(created_at), "
                "CAST(total(lat) * 1000000 AS INTEGER), "
                "CAST(total(lon) * 1000000 AS INTEGER), "
                "CAST(total(CASE stitch_status WHEN 'pending' THEN 1 "
                "WHEN 'ok' THEN 2 WHEN 'failed' THEN 3 ELSE 0 END) AS INTEGER), "
                "CAST(total(CASE stitch_projection WHEN 'equirectangular' THEN 1 "
                "WHEN 'flat' THEN 2 ELSE 0 END) AS INTEGER), "
                "(SELECT COUNT(*) FROM files f "
                "JOIN favorites fv ON fv.sha256 = f.sha256 "
                "WHERE f.status='organized' "
                "AND f.lat IS NOT NULL AND f.lon IS NOT NULL), "
                "(SELECT CAST(total(f.id) AS INTEGER) FROM files f "
                "JOIN favorites fv ON fv.sha256 = f.sha256 "
                "WHERE f.status='organized' "
                "AND f.lat IS NOT NULL AND f.lon IS NOT NULL) "
                "FROM files "
                "WHERE status='organized' AND lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchone()
            max_id, count, max_created = (ver[0] or 0), ver[1], ver[2]
            # The `lib4-` token is a PAYLOAD-SCHEMA version: bump it whenever the feature
            # `properties` shape changes (v4: `is_favorite` was added) so a client
            # holding a `no-cache`-revalidated response from the previous build gets a
            # one-time 200 with the new field instead of a 304 serving the old body.
            etag = (f'W/"lib4-{max_id}-{count}-{ver[3]}-{ver[4]}-{ver[5]}-{ver[6]}'
                    f'-{ver[7]}-{ver[8]}"')
            inm = request.headers.get("if-none-match")
            if inm is not None and _etag_matches(inm, etag):
                return Response(
                    status_code=304,
                    headers={
                        "ETag": etag,
                        "Cache-Control": "no-cache",
                        "Vary": "Accept-Encoding",
                    },
                )
            rows = conn.execute(
                "SELECT id, filename, place_string, local_date, capture_ts_local, "
                "media_type, codec, "
                "gps_source, capture_kind, frame_count, star_rating, stitch_status, "
                "stitch_projection, dest_path, lat, lon, "
                "EXISTS(SELECT 1 FROM favorites WHERE sha256 = files.sha256) "
                "AS is_favorite "
                "FROM files WHERE status='organized' AND lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchall()
            # Videos with an SRT telemetry sidecar have a drawable flight track
            # (GET /api/track/{id}); the UI shows its button only when this is set.
            srt_ids = {
                r["primary_file_id"]
                for r in conn.execute(
                    "SELECT DISTINCT primary_file_id FROM file_companions "
                    "WHERE companion_type='srt'"
                )
            }
        finally:
            conn.close()
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "id": r["id"],
                    "filename": r["filename"],
                    "place_string": r["place_string"],
                    "local_date": r["local_date"],
                    "capture_ts_local": r["capture_ts_local"],
                    "media_type": r["media_type"],
                    "codec": r["codec"],
                    "gps_source": r["gps_source"],
                    "capture_kind": r["capture_kind"],
                    "frame_count": r["frame_count"],
                    "star_rating": r["star_rating"],
                    "stitch_status": r["stitch_status"],
                    "stitch_projection": r["stitch_projection"],
                    "has_track": r["media_type"] == "video" and r["id"] in srt_ids,
                    "is_favorite": bool(r["is_favorite"]),
                    "path": _relpath(r["dest_path"], url_root, library_root),
                },
            }
            for r in rows
        ]
        body = json.dumps(
            {"type": "FeatureCollection", "features": features}
        ).encode("utf-8")
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache",  # always revalidate (send If-None-Match)
            "Vary": "Accept-Encoding",
        }
        last_modified = _http_date(max_created)
        if last_modified is not None:
            headers["Last-Modified"] = last_modified
        # GZip scoped to THIS route only (8-12 MB -> ~1-2 MB at 20k); a global
        # GZipMiddleware would also compress the video FileResponse and break HTTP
        # Range seeking, so the JSON feed compresses itself here instead.
        if "gzip" in request.headers.get("accept-encoding", "").lower():
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return Response(content=body, media_type="application/json", headers=headers)

    @app.get("/api/frames/{file_id}")
    def frames(file_id: int) -> dict:
        """List a capture's source-frame relpaths for the lightbox gallery.

        Serves both a hyperlapse render's ``hyperlapse_frame`` companions (B10) and
        a panorama primary's ``panorama_frame`` tiles (B12).
        """
        conn = _index()
        try:
            if conn.execute(
                "SELECT 1 FROM files WHERE id=?", (file_id,)
            ).fetchone() is None:
                raise HTTPException(status_code=404, detail="unknown file")
            rows = conn.execute(
                "SELECT dest_path FROM file_companions "
                "WHERE primary_file_id=? "
                "AND companion_type IN ('hyperlapse_frame', 'panorama_frame') "
                "ORDER BY dest_path",
                (file_id,),
            ).fetchall()
        finally:
            conn.close()
        return {"frames": [_relpath(r["dest_path"], url_root, library_root) for r in rows]}

    @app.get("/api/track/{file_id}")
    def track(file_id: int) -> dict:
        """A video's GPS flight track, parsed from its SRT telemetry sidecar.

        ``points`` retains the original GeoJSON-order route contract. ``samples``
        adds subtitle-clock-aligned fixes for synchronized playback, each with the
        frame's ``alt`` in metres (``null`` when that cue carries no altitude
        token). Each list is independently downsampled to at most
        ``_TRACK_MAX_POINTS`` while retaining its first and final item.

        ``altitude_ref`` labels the datum every ``alt`` is measured against —
        ``'relative'`` (above the takeoff point) or ``'absolute'`` (barometric
        MSL) — and is ``null`` when the sidecar carries no altitude at all. It is
        taken from the first sample with a height: DJI pins one payload family
        per file, so the datum does not change mid-track.

        Empty data is not an error; 404 is reserved for an unknown file id.
        """
        conn = _index()
        try:
            if conn.execute(
                "SELECT 1 FROM files WHERE id=?", (file_id,)
            ).fetchone() is None:
                raise HTTPException(status_code=404, detail="unknown file")
            row = conn.execute(
                "SELECT dest_path FROM file_companions "
                "WHERE primary_file_id=? AND companion_type='srt' LIMIT 1",
                (file_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"points": [], "samples": [], "altitude_ref": None}
        path = _strip(row["dest_path"])
        fixes = _downsample_track(srt_parser.parse_srt_track(path))
        samples = _downsample_track(srt_parser.parse_srt_track_samples(path))
        return {
            "points": [[lon, lat] for lat, lon in fixes],
            "samples": [
                {
                    "time_s": sample.time_s,
                    "lon": sample.lon,
                    "lat": sample.lat,
                    "alt": sample.alt,
                }
                for sample in samples
            ],
            "altitude_ref": next(
                (s.alt_ref for s in samples if s.alt is not None), None
            ),
        }

    @app.get("/api/inbox")
    def inbox_count() -> dict:
        return asdict(inbox.count_inbox(cfg.inbox_path))

    @app.get("/api/inbox/list")
    def inbox_list() -> dict:
        return {"groups": [asdict(g) for g in inbox.list_inbox(cfg.inbox_path)]}

    @app.get("/api/quarantine")
    def quarantine() -> dict:
        """List no-GPS (quarantined) captures so the map UI can set their location.

        These rows are excluded from ``/api/library`` (no coordinate to plot), so the
        No-GPS panel fetches them here. ``date`` is the stored ``local_date`` when
        present, else the ``_no-gps/<date>/`` quarantine folder name.
        """
        conn = _index()
        try:
            rows = conn.execute(
                "SELECT id, filename, media_type, local_date, dest_path, "
                "capture_kind, frame_count "
                "FROM files WHERE status='quarantined' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        features = []
        for r in rows:
            date = r["local_date"]
            if date is None:
                parent = Path(_strip(r["dest_path"])).parent.name
                date = parent if parent != "_no-gps" else None
            features.append({
                "id": r["id"],
                "filename": r["filename"],
                "media_type": r["media_type"],
                "date": date,
                "capture_kind": r["capture_kind"],
                "frame_count": r["frame_count"],
                "path": _relpath(r["dest_path"], url_root, library_root),
            })
        return {"features": features}

    @app.get("/api/duplicates")
    def duplicates_list() -> dict:
        """List the pending re-import duplicates so the map UI can drain the backlog.

        With ``relocate_duplicates = false`` a detected duplicate stays in the inbox;
        organize records it in the ``duplicates`` table (see
        :mod:`geosorter.duplicates`). ``source_path`` is inbox-relative POSIX (bare
        filename if the stored path is not under the configured inbox);
        ``matched_path`` is the matched library file, library-relative like every
        media URL, or null when the match is gone; ``missing`` flags a source that
        has vanished from disk (dismiss then just deletes the row).
        """
        inbox_root = Path(cfg.inbox_path) if cfg.inbox_path else None
        conn = _index()
        try:
            rows = duplicates.list_rows(conn)
        finally:
            conn.close()
        items = []
        for r in rows:
            src = Path(_strip(r["source_path"]))
            try:
                rel = src.relative_to(inbox_root).as_posix() if inbox_root else src.name
            except ValueError:
                rel = src.name
            matched = r["matched_dest_path"]
            items.append({
                "id": r["id"],
                "filename": src.name,
                "source_path": rel,
                "matched_path": (
                    _relpath(matched, url_root, library_root) if matched else None
                ),
                "matched_file_id": r["matched_file_id"],
                "sha256": r["sha256"],
                "first_seen_at": r["first_seen_at"],
                "missing": not src.exists(),
            })
        return {"items": items, "count": len(items)}

    @app.post("/api/duplicates/dismiss", dependencies=[Depends(require_admin)])
    def duplicates_dismiss(req: DismissDuplicatesRequest) -> dict:
        """Move the selected duplicate groups into ``_duplicates/`` and drop their rows.

        Synchronous — renames within the inbox volume are cheap, so no job is needed.
        Refused with 409 (same shape as :func:`_submit_or_409`) while any destructive
        job is in flight: moving inbox files under a live organize is racy. Also 409
        (same detail shape, null ``blocking_job_id``) when no ``inbox_path`` is
        configured — there is no inbox to relocate into.
        """
        blocking = jobs.active_destructive_job_id()
        if blocking is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "a destructive job is already running",
                    "blocking_job_id": blocking,
                },
            )
        if cfg.inbox_path is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "no inbox_path configured; cannot relocate duplicates",
                    "blocking_job_id": None,
                },
            )
        conn = _index()
        try:
            result = duplicates.dismiss(conn, req.ids, Path(cfg.inbox_path))
        finally:
            conn.close()
        return {
            "dismissed": result.dismissed,
            "skipped": result.skipped,
            "failures": result.failures,
        }

    @app.post("/api/favorite", dependencies=[Depends(require_admin)])
    def favorite(req: FavoriteRequest) -> dict:
        """Set/clear the favorite flag for a file's content hash (idempotent).

        Keyed on ``sha256``, not ``files.id`` — ids die on undo/re-import; the hash
        is the stable identity organize itself dedupes on, so a favorite survives an
        undo + re-import cycle.
        """
        conn = _index()
        try:
            row = conn.execute(
                "SELECT sha256 FROM files WHERE id=?", (req.file_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="unknown file")
            if req.favorite:
                conn.execute(
                    "INSERT OR IGNORE INTO favorites(sha256) VALUES (?)",
                    (row["sha256"],),
                )
            else:
                conn.execute("DELETE FROM favorites WHERE sha256=?", (row["sha256"],))
            conn.commit()
        finally:
            conn.close()
        return {"file_id": req.file_id, "favorite": req.favorite}

    @app.get("/api/place-search")
    def place_search(q: str = "") -> dict:
        """Offline forward geocoding: resolve a place/feature name to coordinates.

        Returns ranked ``{geonameid, name, place_string, lat, lon, feature_class}``
        matches from the GeoNames DB so the user can assign a no-GPS capture by name
        instead of a map click. Blank query or an absent GeoNames DB -> ``[]``.
        """
        if not q.strip() or not Path(cfg.geonames_db_path).is_file():
            return {"results": []}
        conn = db.connect(cfg.geonames_db_path, integrity_check=False)
        try:
            matches = geocoder.forward_search(conn, q)
        finally:
            conn.close()
        return {"results": [asdict(m) for m in matches]}

    @app.get("/api/auth")
    def auth_status() -> dict:
        """Whether admin login is required (a password is configured)."""
        return {"auth_required": auth_configured}

    @app.post("/api/login")
    def login(req: LoginRequest, request: Request) -> dict:
        """Exchange the admin password for a bearer token (429 while throttled)."""
        if not auth_configured:
            raise HTTPException(status_code=400, detail="admin auth not configured")
        client_key = request.client.host if request.client else "unknown"
        wait = login_throttle.retry_after(client_key)
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail="too many failed login attempts; try again later",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        if not auth.verify_password(req.password, cfg.admin_password_hash):
            login_throttle.record_failure(client_key)
            raise HTTPException(status_code=401, detail="invalid password")
        login_throttle.record_success(client_key)
        return {"token": tokens.issue()}

    @app.post("/api/logout")
    def logout(authorization: str | None = Header(default=None)) -> dict:
        """Revoke the presented bearer token (idempotent)."""
        tokens.revoke(_bearer(authorization))
        return {"ok": True}

    @app.post("/api/organize", dependencies=[Depends(require_admin)])
    def organize_start(req: OrganizeRequest = OrganizeRequest()) -> dict:
        selected = set(req.primaries) if req.primaries is not None else None
        return _submit_or_409(lambda: jobs.submit(selected_primaries=selected))

    @app.get("/api/organize/status/{job_id}")
    def organize_status(job_id: str) -> dict:
        state = jobs.status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/organize/cancel/{job_id}", dependencies=[Depends(require_admin)])
    def organize_cancel(job_id: str) -> dict:
        if jobs.status(job_id) is None or not jobs.cancel(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return {"cancelled": True}

    @app.post("/api/undo", dependencies=[Depends(require_admin)])
    def undo_start() -> dict:
        return _submit_or_409(jobs.submit_undo)

    @app.get("/api/undo/status/{job_id}")
    def undo_status(job_id: str) -> dict:
        state = jobs.undo_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/undo/cancel/{job_id}", dependencies=[Depends(require_admin)])
    def undo_cancel(job_id: str) -> dict:
        if jobs.undo_status(job_id) is None or not jobs.cancel(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return {"cancelled": True}

    @app.post("/api/retag", dependencies=[Depends(require_admin)])
    def retag_start(req: RetagRequest) -> dict:
        return _submit_or_409(lambda: jobs.submit_retag(req.file_id, req.lat, req.lon))

    @app.get("/api/retag/status/{job_id}")
    def retag_status(job_id: str) -> dict:
        state = jobs.retag_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/assign-location", dependencies=[Depends(require_admin)])
    def assign_location_start(req: AssignRequest) -> dict:
        return _submit_or_409(
            lambda: jobs.submit_assign(req.file_ids, req.lat, req.lon)
        )

    @app.get("/api/assign-location/status/{job_id}")
    def assign_location_status(job_id: str) -> dict:
        state = jobs.assign_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/rescan", dependencies=[Depends(require_admin)])
    def rescan_start() -> dict:
        return _submit_or_409(jobs.submit_rescan)

    @app.get("/api/rescan/status/{job_id}")
    def rescan_status(job_id: str) -> dict:
        state = jobs.rescan_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    # ----- broken-capture repair (m-repair-broken-captures) ------------------ #

    @app.get("/api/repair/untrunc")
    def repair_untrunc() -> dict:
        """Whether an untrunc executable is available (config path or PATH)."""
        path = repair.find_untrunc(cfg.untrunc_path)
        return {"available": path is not None, "path": path}

    @app.post("/api/repair/scan", dependencies=[Depends(require_admin)])
    def repair_scan_start() -> dict:
        return {"job_id": jobs.submit_repair_scan()}

    @app.get("/api/repair/scan/status/{job_id}")
    def repair_scan_status(job_id: str) -> dict:
        state = jobs.repair_scan_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.get("/api/repair/references/{file_id}")
    def repair_references(file_id: int) -> dict:
        """Ranked healthy reference clips for one broken capture (best first)."""
        try:
            candidates = repair.reference_candidates(cfg, file_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="unknown file")
        return {
            "candidates": [
                {
                    "id": c.file_id,
                    "filename": c.filename,
                    "path": c.rel_path,
                    "date": c.date,
                    "place_string": c.place_string,
                    "codec": c.codec,
                    "width": c.width,
                    "height": c.height,
                    "duration_s": c.duration_s,
                    "score": c.score,
                    "reasons": c.reasons,
                    "recommended": c.recommended,
                }
                for c in candidates
            ]
        }

    @app.post("/api/repair/run", dependencies=[Depends(require_admin)])
    def repair_run(req: RepairRunRequest) -> dict:
        if repair.find_untrunc(cfg.untrunc_path) is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "untrunc is not installed — set untrunc_path in "
                               "geosorter.toml or put it on PATH",
                    "blocking_job_id": None,
                },
            )
        conn = _index()
        try:
            known = {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM files WHERE id IN (?, ?)",
                    (req.file_id, req.reference_id),
                )
            }
        finally:
            conn.close()
        if req.file_id not in known or req.reference_id not in known:
            raise HTTPException(status_code=404, detail="unknown file")
        return {"job_id": jobs.submit_repair(req.file_id, req.reference_id)}

    @app.get("/api/repair/status/{job_id}")
    def repair_status(job_id: str) -> dict:
        state = jobs.repair_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    def _repair_sync_guard(file_id: int) -> None:
        """409 when the destructive worker, or a repair of this file, is in flight.

        The synchronous accept/discard/delete steps mutate library files and (for
        accept/delete) the index — never under a live organize/undo/retag/rescan
        (same contract as duplicates/dismiss), and never while untrunc is still
        writing this very capture's work files.
        """
        blocking = jobs.active_destructive_job_id() or jobs.active_repair_job_for(
            file_id
        )
        if blocking is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "a conflicting job is already running",
                    "blocking_job_id": blocking,
                },
            )

    def _repair_sync_call(fn, file_id: int) -> dict:
        """Run one synchronous repair step, mapping ValueError to a clean 4xx.

        An unknown id is a 404; every other refusal (no output awaiting
        acceptance, a file that re-probes healthy, failed verification) is a 409
        carrying the human-readable reason.
        """
        try:
            return fn(cfg, file_id)
        except ValueError as exc:
            if "unknown file id" in str(exc):
                raise HTTPException(status_code=404, detail="unknown file")
            raise HTTPException(
                status_code=409,
                detail={"message": str(exc), "blocking_job_id": None},
            )

    @app.post("/api/repair/accept", dependencies=[Depends(require_admin)])
    def repair_accept(req: RepairFileRequest) -> dict:
        """Swap the verified ``_repair/fixed/`` output onto the capture's dest path."""
        _repair_sync_guard(req.file_id)
        return _repair_sync_call(repair.accept_repair, req.file_id)

    @app.post("/api/repair/discard", dependencies=[Depends(require_admin)])
    def repair_discard(req: RepairFileRequest) -> dict:
        """Drop an unaccepted repair attempt's work files (output + backup)."""
        _repair_sync_guard(req.file_id)
        return _repair_sync_call(repair.discard_repair, req.file_id)

    @app.post("/api/repair/delete", dependencies=[Depends(require_admin)])
    def repair_delete(req: RepairFileRequest) -> dict:
        """Delete a broken capture from disk + prune its rows (server re-verifies)."""
        _repair_sync_guard(req.file_id)
        return _repair_sync_call(repair.delete_broken, req.file_id)

    def _panorama_row(file_id: int):
        """Return the panorama primary's row, or raise 404 (unknown/non-panorama)."""
        conn = _index()
        try:
            row = conn.execute(
                "SELECT dest_path, capture_kind FROM files WHERE id=?", (file_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["capture_kind"] != "panorama":
            raise HTTPException(status_code=404, detail="not a panorama")
        return row

    @app.post("/api/stitch/{file_id}", dependencies=[Depends(require_admin)])
    def stitch_start(file_id: int, req: StitchRequest = StitchRequest()) -> dict:
        """Kick off the (lazy, ~7-min, dedicated-pool) Hugin stitch for a panorama.

        An optional body ``{force, projection}`` drives a manual re-stitch (the map UI's
        re-stitch control): ``force`` re-runs cold, ``projection`` overrides auto-detect.
        """
        _panorama_row(file_id)
        return {
            "job_id": jobs.submit_stitch(
                file_id, force=req.force, projection=req.projection
            )
        }

    @app.get("/api/stitch/status/{job_id}")
    def stitch_status(job_id: str) -> dict:
        state = jobs.stitch_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.get("/api/stitch/{file_id}")
    def stitch_image(file_id: int) -> FileResponse:
        """Serve the cached stitched hero, or 404 so the client uses the gallery.

        Path is derived server-side from the panorama primary's stored ``dest_path``
        (never a client relpath) and always a ``.jpg``, so ``.pto``/intermediate
        artifacts are structurally unservable.
        """
        row = _panorama_row(file_id)
        rel_key = pathing.library_rel_key(url_root, row["dest_path"])
        out = derived.stitch_cache_path(proxy_cache_dir, rel_key)
        if not out.is_file():
            raise HTTPException(status_code=404, detail="stitch not generated")
        return FileResponse(_safe_cache_path(out, proxy_cache_dir), media_type="image/jpeg")

    @app.get("/api/collage/{file_id}")
    def collage_image(file_id: int) -> FileResponse:
        """Serve the instant raw-tile collage for a panorama (Pillow, no Hugin).

        Generated on first request (like ``/api/thumb``) from the primary tile + its
        ``panorama_frame`` companions, cached on the LOCAL ``cache_dir`` tier. The
        lightbox shows it immediately while the optional Hugin stitch is absent or
        still running. Path is server-derived from the stored ``dest_path``s (never a
        client relpath) and always a ``.jpg``.
        """
        row = _panorama_row(file_id)
        primary = _strip(row["dest_path"])
        conn = _index()
        try:
            frames = [
                _strip(r["dest_path"])
                for r in conn.execute(
                    "SELECT dest_path FROM file_companions "
                    "WHERE primary_file_id=? AND companion_type='panorama_frame' "
                    "ORDER BY dest_path",
                    (file_id,),
                )
            ]
        finally:
            conn.close()
        rel_key = pathing.library_rel_key(url_root, row["dest_path"])
        out = derived.panorama_collage(cache_dir, rel_key, primary, frames)
        return FileResponse(_safe_cache_path(out, cache_dir), media_type="image/jpeg")

    @app.get("/api/media/{relpath:path}")
    def media(relpath: str) -> FileResponse:
        return FileResponse(_safe_path(relpath))  # range-capable

    @app.get("/api/thumb/{relpath:path}")
    def thumb(relpath: str) -> FileResponse:
        src = _safe_path(relpath)
        out = _gen_or_http(lambda: derived.thumbnail(cache_dir, relpath, src))
        return FileResponse(_safe_cache_path(out, cache_dir), media_type="image/jpeg")

    @app.get("/api/preview/{relpath:path}")
    def preview(relpath: str) -> FileResponse:
        src = _safe_path(relpath)
        out = _gen_or_http(lambda: derived.preview(cache_dir, relpath, src))
        return FileResponse(_safe_cache_path(out, cache_dir), media_type="image/jpeg")

    @app.get("/api/poster/{relpath:path}")
    def poster(relpath: str) -> FileResponse:
        src = _safe_path(relpath)
        out = _gen_or_http(lambda: derived.poster(cache_dir, relpath, src))
        return FileResponse(_safe_cache_path(out, cache_dir), media_type="image/jpeg")

    @app.get("/api/video/{relpath:path}")
    def video(relpath: str) -> FileResponse:
        source = _safe_path(relpath)
        out = _gen_or_http(lambda: derived.proxy(proxy_cache_dir, relpath, source,
                                                 _lookup_codec(relpath),
                                                 hwaccel=cfg.proxy_hwaccel))
        # An h264 source is returned unchanged (under library_root); a proxy lives
        # under proxy_cache_dir — allow either.
        return FileResponse(_safe_cache_path(out, proxy_cache_dir, library_root))

    def _lookup_codec(relpath: str) -> str | None:
        # Indexed equality on the UNIQUE dest_path instead of scanning every video
        # row + recomputing _relpath. The URL relpath came FROM _relpath (strip
        # \\?\ -> relative_to(url_root) -> POSIX), so str(url_root / relpath)
        # reconstructs the stored form; query both the plain (tests) and \\?\-
        # prefixed (production, compute_dest_path) variants.
        candidate = str(Path(url_root) / relpath)
        conn = _index()
        try:
            row = conn.execute(
                "SELECT codec FROM files WHERE media_type='video' "
                "AND dest_path IN (?, ?)",
                (candidate, pathing.add_long_prefix(candidate)),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return row["codec"]
        # A repaired-output preview (`_repair/fixed/...`) has no index row yet —
        # probe the file directly so an HEVC rebuild still gets its playable
        # proxy for the pre-accept verification player.
        if relpath.replace("\\", "/").startswith(repair.REPAIR_DIRNAME + "/"):
            return repair.probe_file(candidate).codec
        return None

    # Same-origin SPA (B7 build output); mounted last so /api routes win. Only
    # mounted when the build directory exists, so B6 alone serves a bare API.
    spa = Path(spa_dir) if spa_dir is not None else Path(__file__).parent / "webui"
    if spa.is_dir():
        app.mount("/", StaticFiles(directory=str(spa), html=True), name="spa")

    return app
