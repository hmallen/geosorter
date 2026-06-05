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
* ``GET  /api/media/{relpath}`` — original file, range-capable (video seek),
  path-traversal-guarded.
* ``GET  /api/thumb/{relpath}`` / ``GET /api/poster/{relpath}`` — lazily generated,
  cached derived images (see :mod:`geosorter.derived`).
* ``GET  /api/video/{relpath}`` — a browser-playable video: H.264 originals served
  directly, HEVC served as a cached H.264 proxy.

The app binds to ``127.0.0.1`` (the ``serve`` CLI verb owns the socket); there is
no auth, so the GeoJSON — which embeds home GPS coordinates — never leaves the
loopback interface unless the operator explicitly opts in via ``--host``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from . import db, derived, inbox
from .jobs import JobManager

logger = logging.getLogger("geosorter.api")


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


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


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
    jobs = job_manager if job_manager is not None else JobManager(cfg)

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
    def library() -> dict:
        conn = _index()
        try:
            rows = conn.execute(
                "SELECT id, filename, place_string, local_date, media_type, codec, "
                "gps_source, capture_kind, frame_count, star_rating, stitch_status, "
                "dest_path, lat, lon "
                "FROM files WHERE status='organized' AND lat IS NOT NULL AND lon IS NOT NULL"
            ).fetchall()
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
                    "media_type": r["media_type"],
                    "codec": r["codec"],
                    "gps_source": r["gps_source"],
                    "capture_kind": r["capture_kind"],
                    "frame_count": r["frame_count"],
                    "star_rating": r["star_rating"],
                    "stitch_status": r["stitch_status"],
                    "path": _relpath(r["dest_path"], url_root, library_root),
                },
            }
            for r in rows
        ]
        return {"type": "FeatureCollection", "features": features}

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

    @app.get("/api/inbox")
    def inbox_count() -> dict:
        return asdict(inbox.count_inbox(cfg.inbox_path))

    @app.get("/api/inbox/list")
    def inbox_list() -> dict:
        return {"groups": [asdict(g) for g in inbox.list_inbox(cfg.inbox_path)]}

    @app.post("/api/organize")
    def organize_start(req: OrganizeRequest = OrganizeRequest()) -> dict:
        selected = set(req.primaries) if req.primaries is not None else None
        return {"job_id": jobs.submit(selected_primaries=selected)}

    @app.get("/api/organize/status/{job_id}")
    def organize_status(job_id: str) -> dict:
        state = jobs.status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/organize/cancel/{job_id}")
    def organize_cancel(job_id: str) -> dict:
        if jobs.status(job_id) is None or not jobs.cancel(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return {"cancelled": True}

    @app.post("/api/undo")
    def undo_start() -> dict:
        return {"job_id": jobs.submit_undo()}

    @app.get("/api/undo/status/{job_id}")
    def undo_status(job_id: str) -> dict:
        state = jobs.undo_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

    @app.post("/api/undo/cancel/{job_id}")
    def undo_cancel(job_id: str) -> dict:
        if jobs.undo_status(job_id) is None or not jobs.cancel(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return {"cancelled": True}

    @app.post("/api/retag")
    def retag_start(req: RetagRequest) -> dict:
        return {"job_id": jobs.submit_retag(req.file_id, req.lat, req.lon)}

    @app.get("/api/retag/status/{job_id}")
    def retag_status(job_id: str) -> dict:
        state = jobs.retag_status(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(state)

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

    @app.post("/api/stitch/{file_id}")
    def stitch_start(file_id: int) -> dict:
        """Kick off the (lazy, ~7-min, dedicated-pool) Hugin stitch for a panorama."""
        _panorama_row(file_id)
        return {"job_id": jobs.submit_stitch(file_id)}

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
        out = derived.stitch_cache_path(library_root, Path(_strip(row["dest_path"])))
        if not out.is_file():
            raise HTTPException(status_code=404, detail="stitch not generated")
        return FileResponse(out, media_type="image/jpeg")

    @app.get("/api/media/{relpath:path}")
    def media(relpath: str) -> FileResponse:
        return FileResponse(_safe_path(relpath))  # range-capable

    @app.get("/api/thumb/{relpath:path}")
    def thumb(relpath: str) -> FileResponse:
        out = derived.thumbnail(library_root, _safe_path(relpath))
        return FileResponse(out, media_type="image/jpeg")

    @app.get("/api/preview/{relpath:path}")
    def preview(relpath: str) -> FileResponse:
        out = derived.preview(library_root, _safe_path(relpath))
        return FileResponse(out, media_type="image/jpeg")

    @app.get("/api/poster/{relpath:path}")
    def poster(relpath: str) -> FileResponse:
        out = derived.poster(library_root, _safe_path(relpath))
        return FileResponse(out, media_type="image/jpeg")

    @app.get("/api/video/{relpath:path}")
    def video(relpath: str) -> FileResponse:
        source = _safe_path(relpath)
        out = derived.proxy(library_root, source, _lookup_codec(relpath))
        return FileResponse(out)  # range-capable

    def _lookup_codec(relpath: str) -> str | None:
        conn = _index()
        try:
            for row in conn.execute(
                "SELECT dest_path, codec FROM files WHERE media_type='video'"
            ):
                if _relpath(row["dest_path"], url_root, library_root) == relpath:
                    return row["codec"]
        finally:
            conn.close()
        return None

    # Same-origin SPA (B7 build output); mounted last so /api routes win. Only
    # mounted when the build directory exists, so B6 alone serves a bare API.
    spa = Path(spa_dir) if spa_dir is not None else Path(__file__).parent / "webui"
    if spa.is_dir():
        app.mount("/", StaticFiles(directory=str(spa), html=True), name="spa")

    return app
