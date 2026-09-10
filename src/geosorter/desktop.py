"""Local desktop shell; deliberately separate from the network-facing serve app."""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import shutil
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from . import __version__, api, auth, bootstrap, config, db, derived, repair
from .desktop_config import (checked_folder, index_version, mark_upgrade, prepare_upgrade,
                             read_json, save_config, save_json, selected_config, validate_folders)
from .jobs import JobManager

logger = logging.getLogger(__name__)


def dependency_checks() -> list[dict]:
    results = []
    for tool, args in (("exiftool", ["-ver"]), ("ffprobe", ["-version"]), ("ffmpeg", ["-encoders"])):
        try:
            executable = shutil.which(tool)
            if not executable:
                raise ValueError(f"{tool} is missing. Reinstall GeoSorter or check the selected tool installation.")
            proc = subprocess.run([executable, *args], capture_output=True, text=True, timeout=15,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode:
                raise ValueError(f"{tool} could not start. Reinstall GeoSorter and try again.")
            if tool == "exiftool" and tuple(int(x) for x in proc.stdout.strip().split(".")[:2]) < (12, 24):
                raise ValueError("ExifTool must be version 12.24 or newer. Reinstall GeoSorter.")
            if tool == "ffmpeg" and "libx264" not in proc.stdout:
                raise ValueError("FFmpeg does not include the H.264 encoder. Reinstall GeoSorter.")
            results.append({"name": tool, "ok": True, "message": "Ready"})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            results.append({"name": tool, "ok": False, "message": str(exc)})
    return results


def validate_hugin(path):
    tools = derived.find_hugin(path)
    if not tools:
        raise ValueError("Select Hugin's bin folder containing every required panorama tool.")
    for name, executable in tools.items():
        try:
            result = subprocess.run([executable, "--help"], capture_output=True, text=True,
                                    errors="replace", timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # Hugin command-line programs use either 0 or 1 for usage output.
            if result.returncode not in (0, 1) or not (result.stdout + result.stderr).strip():
                raise ValueError(f"{name} could not run. Reinstall Hugin, then check again.")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"{name} could not run. Reinstall Hugin, then check again.") from exc
    return tools


class DesktopController:
    def __init__(self, config_path=None, *, state_dir=None, checks=dependency_checks,
                 choose_path=None, on_quit=None, on_activate=None, bootstrap_fn=bootstrap.run):
        self.state_dir = Path(state_dir) if state_dir else config.default_data_dir() / "desktop"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = selected_config(config_path, self.state_dir)
        self.checks_fn = checks
        self.choose_path = choose_path or (lambda kind: None)
        self.on_quit = on_quit or (lambda: None)
        self.on_activate = on_activate or (lambda view: None)
        self.bootstrap_fn = bootstrap_fn
        self.secret = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.tokens = auth.TokenStore()
        self.throttle = auth.LoginThrottle()
        self.lock = threading.RLock()
        self.idle_condition = threading.Condition(self.lock)
        self.api_writes = 0
        self.jobs = None
        self.library = None
        self.cfg = None
        self.error = None
        self.error_details = None
        self.checks = []
        self.hugin_ready = False
        self.hugin_message = None
        self.closing = False
        self.closed = False
        self.job = read_json(self.state_dir / "setup-job.json") or None
        if self.job and self.job["state"] in ("pending", "running"):
            self.job.update(state="interrupted", message="Setup was interrupted. Retry to continue from saved downloads.")
            self._persist_job()
        self.worker = None
        self.recheck()

    def _persist_job(self):
        save_json(self.state_dir / "setup-job.json", self.job)

    def active(self):
        with self.lock:
            active = self.jobs.active_jobs() if self.jobs else []
            if self.api_writes:
                active.append({"job_id": "requests", "kind": "saving changes", "state": "running"})
            if self.job and self.job["state"] in ("pending", "running"):
                active.append({"job_id": self.job["job_id"], "kind": self.job["kind"], "state": self.job["state"]})
            return active

    def ensure_idle(self):
        if self.closing or self.active():
            raise ValueError("GeoSorter is busy. Wait for current work to finish and try again.")

    def unload(self):
        if self.jobs:
            self.jobs.shutdown()
        self.jobs = None
        self.library = None

    def recheck(self):
        with self.lock:
            if self.active() or self.closing:
                return
            self.unload()
            self.error = None
            self.error_details = None
            try:
                self.cfg = config.load(self.config_path)
                self.checks = self.checks_fn()
                self.hugin_ready = False
                self.hugin_message = None
                if derived.find_hugin(self.cfg.hugin_bin_dir):
                    try:
                        validate_hugin(self.cfg.hugin_bin_dir)
                        self.hugin_ready = True
                    except ValueError as exc:
                        self.hugin_message = str(exc)
                if not self.cfg.inbox_path or not self.cfg.library_root:
                    return
                validate_folders(self.cfg.inbox_path, self.cfg.library_root, new=False)
                if not self.cfg.index_db_path.is_file():
                    raise ValueError("The catalog is missing. Restore its backup or select your original configuration; GeoSorter will not create an empty replacement.")
                if not all(check["ok"] for check in self.checks) or not bootstrap.ready(self.cfg.geonames_db_path):
                    return
                prepare_upgrade(self.cfg, self.config_path, self.state_dir)
                manager = JobManager(self.cfg)
                try:
                    library = api.create_app(self.cfg, job_manager=manager, token_store=self.tokens)
                except Exception:
                    manager.shutdown()
                    raise
                mark_upgrade(self.cfg, self.state_dir)
                self.jobs, self.library = manager, library
            except Exception as exc:
                self.error = str(exc)
                self.error_details = f"{type(exc).__name__}: {exc}"
                logger.exception("Desktop readiness check failed")

    def snapshot(self):
        with self.lock:
            cfg = self.cfg
            configured = bool(cfg and cfg.inbox_path and cfg.library_root)
            folders_ok = bool(configured and cfg.inbox_path.is_dir() and cfg.library_root.is_dir())
            geo = bool(cfg and bootstrap.ready(cfg.geonames_db_path))
            state = "ready" if self.library is not None and folders_ok else "setup"
            if self.error or (configured and not folders_ok) or any(not c["ok"] for c in self.checks):
                state = "recovery"
            if self.closing:
                state = "closing"
            return {"state": state, "version": __version__, "configured": configured,
                    "config_path": str(self.config_path), "inbox_path": str(cfg.inbox_path) if cfg and cfg.inbox_path else "",
                    "library_root": str(cfg.library_root) if cfg and cfg.library_root else "",
                    "geonames_ready": geo, "checks": self.checks,
                    "error": self.error or ("Connect the configured drives, then choose Retry." if configured and not folders_ok else None),
                    "error_details": self.error_details,
                    "job": self.job, "active_jobs": self.active(),
                    "auth_required": bool(cfg and cfg.admin_password_hash),
                    "extras": {"hugin": self.hugin_ready, "hugin_message": self.hugin_message,
                               "untrunc": bool(cfg and repair.find_untrunc(cfg.untrunc_path))}}

    def authorize(self, request):
        if self.cfg and self.cfg.admin_password_hash:
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not self.tokens.valid(bearer):
                raise HTTPException(401, "Log in as administrator to change settings.")

    def configure(self, incoming, destination, *, apply=False):
        with self.lock:
            self.ensure_idle()
            configured = bool(self.cfg and self.cfg.library_root)
            if configured and Path(destination) != self.cfg.library_root:
                raise ValueError("Moving or switching an established library is not supported in this release.")
            inbox, library = validate_folders(incoming, destination, new=not configured)
            candidate = self.cfg or config.load(self.config_path)
            for path in (candidate.index_db_path, candidate.geonames_db_path, candidate.cache_dir):
                if path and any(path.resolve().is_relative_to(root.resolve()) for root in (inbox, library)):
                    raise ValueError("Catalog, place data, and local cache must be outside both media folders.")
            if not apply:
                return {"ok": True}
            if not configured and candidate.index_db_path.exists():
                raise ValueError("An existing catalog was found. Use its original GeoSorter configuration instead.")
            self.unload()
            if not configured:
                conn = db.connect(candidate.index_db_path)
                try:
                    db.init_index_schema(conn)
                finally:
                    conn.close()
            try:
                save_config(self.config_path, {"inbox_path": inbox, "library_root": library})
            except Exception:
                if not configured:
                    # Only the new, empty catalog created above belongs to this attempt.
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(candidate.index_db_path) + suffix).unlink(missing_ok=True)
                raise
            save_json(self.state_dir / "desktop.json", {"config_path": str(self.config_path)})
            self.recheck()
            return self.snapshot()

    def adopt(self, path, password=""):
        with self.lock:
            self.ensure_idle()
            if self.cfg and self.cfg.library_root and self.library:
                raise ValueError("Switching an established library is not supported in this release.")
            selected = Path(path)
            if not selected.is_absolute() or not selected.is_file():
                raise ValueError("Choose an existing GeoSorter TOML configuration.")
            cfg = config.load(selected)
            if cfg.admin_password_hash:
                if self.throttle.retry_after("adopt"):
                    raise HTTPException(429, "Too many password attempts. Try again later.")
                if not auth.verify_password(password, cfg.admin_password_hash):
                    self.throttle.record_failure("adopt")
                    raise HTTPException(401, "Enter the administrator password for this configuration.")
                self.throttle.record_success("adopt")
            if not cfg.inbox_path or not cfg.library_root:
                raise ValueError("That configuration does not specify both media folders.")
            validate_folders(cfg.inbox_path, cfg.library_root, new=False)
            if index_version(cfg.index_db_path) is None:
                raise ValueError("The original GeoSorter catalog is missing. Restore it before continuing.")
            self.unload()
            self.config_path = selected
            save_json(self.state_dir / "desktop.json", {"config_path": str(selected)})
            self.recheck()
            return self.snapshot()

    def start_job(self, kind):
        with self.lock:
            if kind not in ("cities", "features", "untrunc"):
                raise ValueError("Unknown setup task.")
            if self.job and self.job["state"] in ("pending", "running") and self.job["kind"] == kind:
                return dict(self.job)
            self.ensure_idle()
            if not self.cfg or not self.cfg.library_root:
                raise ValueError("Choose your folders first.")
            self.unload()
            self.job = {"job_id": uuid.uuid4().hex, "kind": kind, "state": "pending",
                        "phase": "preparing", "current": "", "done": 0, "total": 0, "message": None}
            self._persist_job()
            self.worker = threading.Thread(target=self._run_job, args=(kind,), daemon=False)
            self.worker.start()
            return dict(self.job)

    def _run_job(self, kind):
        def progress(phase, current="", done=0, total=0):
            with self.lock:
                changed = self.job["phase"] != phase
                self.job.update(state="running", phase=phase, current=current, done=done, total=total)
                if changed:
                    self._persist_job()
        try:
            progress("preparing")
            if kind in ("cities", "features"):
                self.bootstrap_fn(self.cfg, config_path=self.config_path, features=kind == "features", progress=progress)
            else:
                result = repair.install_untrunc(on_bytes=lambda done, total: progress("downloading", "Video repair", done, total))
                save_config(self.config_path, {"untrunc_path": result.exe_path})
            with self.lock:
                self.job.update(state="done", phase="done", message=None)
        except Exception as exc:
            logger.exception("Setup task %s failed", kind)
            with self.lock:
                self.job.update(state="error", message=f"{exc} Retry when the problem is resolved.")
                self.job["details"] = f"{type(exc).__name__}: {exc}"
        finally:
            with self.lock:
                self._persist_job()
            self.recheck()

    def request_quit(self, wait=False):
        with self.lock:
            if self.active() and not wait:
                return {"busy": True, "active_jobs": self.active()}
            if not self.closing:
                self.closing = True
                threading.Thread(target=self._drain, daemon=False).start()
            return {"busy": False, "closing": True}

    def _drain(self):
        with self.idle_condition:
            self.idle_condition.wait_for(lambda: self.api_writes == 0)
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join()
        if self.jobs:
            self.jobs.shutdown()
        self.closed = True
        self.on_quit()


class FolderRequest(BaseModel):
    inbox_path: str
    library_root: str


class PathRequest(BaseModel):
    path: str
    password: str = ""


class JobRequest(BaseModel):
    kind: str


class QuitRequest(BaseModel):
    wait: bool = False


def create_desktop_app(controller: DesktopController, *, origin: str, spa_dir=None):
    c = controller
    spa = Path(spa_dir) if spa_dir else Path(__file__).parent / "webui"
    static = StaticFiles(directory=spa, html=True, check_dir=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if not c.closed:
            c.closing = True
            await asyncio.to_thread(c._drain)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    gate = asyncio.Lock()

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc), "technical_details": f"{type(exc).__name__}: {exc}"}, status_code=400)

    @app.exception_handler(OSError)
    async def io_error(request, exc):
        logger.exception("Desktop I/O failed")
        return JSONResponse({"detail": "GeoSorter could not access a required file or drive. Check permissions and available space, then retry.",
                             "technical_details": f"{type(exc).__name__}: {exc}"}, status_code=400)

    @app.middleware("http")
    async def session_guard(request, call_next):
        if request.headers.get("host") != origin.removeprefix("http://"):
            return JSONResponse({"detail": "Invalid host"}, status_code=403)
        path = request.url.path
        if request.headers.get("origin") not in (None, origin):
            return JSONResponse({"detail": "Invalid origin"}, status_code=403)
        if path.startswith("/api/"):
            if request.method not in ("GET", "HEAD") and request.headers.get("origin") != origin:
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
            if path not in ("/api/desktop/session", "/api/desktop/activate", "/api/desktop/health"):
                if not hmac.compare_digest(request.cookies.get("geosorter_desktop", ""), c.cookie):
                    return JSONResponse({"detail": "Open GeoSorter from its shortcut to reconnect."}, status_code=401)
            async with gate:
                if c.closing and request.method not in ("GET", "HEAD") and path not in ("/api/desktop/quit", "/api/desktop/session"):
                    return JSONResponse({"detail": "GeoSorter is finishing work before closing."}, status_code=409)
                if path.startswith("/api/desktop/") and request.method != "GET" and path not in (
                    "/api/desktop/session", "/api/desktop/activate", "/api/desktop/login"):
                    try:
                        c.authorize(request)
                    except HTTPException as exc:
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                writing = request.method not in ("GET", "HEAD") and not path.startswith("/api/desktop/")
                with c.lock:
                    if writing and c.closing:
                        return JSONResponse({"detail": "GeoSorter is finishing work before closing."}, status_code=409)
                    if writing:
                        c.api_writes += 1
                try:
                    return await call_next(request)
                finally:
                    if writing:
                        with c.idle_condition:
                            c.api_writes -= 1
                            c.idle_condition.notify_all()
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/api/desktop/health")
    def health():
        return {"desktop": True}

    @app.post("/api/desktop/session")
    def session(body: dict):
        if not hmac.compare_digest(str(body.get("token", "")), c.secret):
            raise HTTPException(401, "Invalid launch token")
        response = JSONResponse({"ok": True})
        response.set_cookie("geosorter_desktop", c.cookie, httponly=True, samesite="strict")
        return response

    @app.post("/api/desktop/activate")
    def activate(body: dict):
        if not hmac.compare_digest(str(body.get("token", "")), c.secret):
            raise HTTPException(401, "Invalid activation token")
        c.on_activate("settings" if body.get("view") == "settings" else "library")
        return {"ok": True}

    @app.get("/api/desktop/status")
    def status():
        return c.snapshot()

    @app.post("/api/desktop/login")
    def login(body: dict):
        if c.throttle.retry_after("desktop"):
            raise HTTPException(429, "Too many password attempts. Try again later.")
        if not c.cfg or not c.cfg.admin_password_hash or not auth.verify_password(str(body.get("password", "")), c.cfg.admin_password_hash):
            c.throttle.record_failure("desktop")
            raise HTTPException(401, "Invalid password")
        c.throttle.record_success("desktop")
        return {"token": c.tokens.issue()}

    @app.post("/api/desktop/retry")
    def retry():
        c.ensure_idle()
        c.recheck()
        return c.snapshot()

    @app.post("/api/desktop/choose")
    def choose(body: dict):
        kind = body.get("kind", "folder")
        if kind not in ("folder", "config", "tool"):
            raise ValueError("Unknown dialog type.")
        return {"path": c.choose_path(kind)}

    @app.post("/api/desktop/create-folder")
    def create_folder(body: PathRequest):
        c.ensure_idle()
        path = Path(body.path)
        if not path.is_absolute() or not path.name:
            raise ValueError("Enter an absolute folder path.")
        checked_folder(path.parent, "parent folder")
        path.mkdir(exist_ok=False)
        return {"path": str(path)}

    @app.post("/api/desktop/validate")
    def validate(body: FolderRequest):
        return c.configure(body.inbox_path, body.library_root)

    @app.post("/api/desktop/configure")
    def configure(body: FolderRequest):
        return c.configure(body.inbox_path, body.library_root, apply=True)

    @app.post("/api/desktop/adopt")
    def adopt(body: PathRequest):
        return c.adopt(body.path, body.password)

    @app.post("/api/desktop/jobs")
    def start_job(body: JobRequest):
        return c.start_job(body.kind)

    @app.get("/api/desktop/jobs/{job_id}")
    def job_status(job_id: str):
        if not c.job or c.job["job_id"] != job_id:
            raise HTTPException(404, "Setup task not found")
        return dict(c.job)

    @app.post("/api/desktop/hugin")
    def hugin(body: PathRequest):
        c.ensure_idle()
        validate_hugin(body.path)
        c.unload()
        save_config(c.config_path, {"hugin_bin_dir": body.path})
        c.recheck()
        return c.snapshot()

    @app.post("/api/desktop/quit")
    def quit_app(body: QuitRequest):
        return c.request_quit(body.wait)

    @app.get("/api/desktop/diagnostics")
    def diagnostics(request: Request):
        c.authorize(request)
        # Deliberately allowlist fields; raw logs/config/media are never exported.
        value = {"version": __version__, "state": c.snapshot()["state"],
                 "tools": [{"name": item["name"], "ok": item["ok"]} for item in c.checks],
                 "setup": {key: c.job[key] for key in ("kind", "state", "phase")} if c.job else None,
                 "active_jobs": c.active()}
        return JSONResponse(value, headers={"Content-Disposition": 'attachment; filename="geosorter-diagnostics.json"'})

    async def dispatch(scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["path"].startswith("/api/"):
            if c.library is None:
                await JSONResponse({"detail": "Complete setup or resolve the startup problem first."}, status_code=503)(scope, receive, send)
                return
            await c.library(scope, receive, send)
        else:
            await static(scope, receive, send)
    app.mount("/", dispatch)
    return app
