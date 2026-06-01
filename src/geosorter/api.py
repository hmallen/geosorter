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

import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from . import db, derived, inbox
from .jobs import JobManager


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


def _relpath(dest_path: str, library_root: Path) -> str:
    """Library-relative POSIX path used in media URLs (best-effort fallback)."""
    stripped = Path(_strip(dest_path))
    try:
        return stripped.relative_to(library_root).as_posix()
    except ValueError:
        return stripped.name


def create_app(cfg, *, spa_dir: Path | str | None = None) -> FastAPI:
    """Build the FastAPI app bound to one :class:`~geosorter.config.Config`."""
    library_root = Path(cfg.library_root).resolve()
    jobs = JobManager(cfg)
    app = FastAPI(title="geosorter", version="0.1.0")

    def _safe_path(relpath: str) -> Path:
        """Resolve a request path under the library or raise (traversal guard)."""
        candidate = (library_root / relpath).resolve()
        if not candidate.is_relative_to(library_root):
            raise HTTPException(status_code=403, detail="path outside library")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return candidate

    def _index():
        conn = db.connect(cfg.index_db_path, integrity_check=False)
        conn.row_factory = sqlite3.Row
        db.init_index_schema(conn)
        return conn

    @app.get("/api/library")
    def library() -> dict:
        conn = _index()
        try:
            rows = conn.execute(
                "SELECT id, filename, place_string, local_date, media_type, codec, "
                "dest_path, lat, lon FROM files "
                "WHERE status='organized' AND lat IS NOT NULL AND lon IS NOT NULL"
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
                    "path": _relpath(r["dest_path"], library_root),
                },
            }
            for r in rows
        ]
        return {"type": "FeatureCollection", "features": features}

    @app.get("/api/inbox")
    def inbox_count() -> dict:
        return asdict(inbox.count_inbox(cfg.inbox_path))

    @app.post("/api/organize")
    def organize_start() -> dict:
        return {"job_id": jobs.submit()}

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
                if _relpath(row["dest_path"], library_root) == relpath:
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
