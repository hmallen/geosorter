"""Windows launcher: owns Tk on the main thread, the instance lock and tray."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time
import webbrowser
from concurrent.futures import Future

import httpx
import uvicorn

from . import config
from .desktop_config import read_json, save_json


class InstanceLock:
    def __init__(self, directory: Path):
        self.directory = directory
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            raise RuntimeError("The desktop launcher currently supports Windows only. Use geosorter serve on this system.")
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        identity = hashlib.sha256(str(self.directory.resolve()).lower().encode()).hexdigest()[:24]
        self.handle = kernel.CreateMutexW(None, True, "Local\\GeoSorter-" + identity)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(self.handle)
            self.handle = None
            return False
        return True

    def close(self):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def listening_socket(preferred=8000):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", preferred))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def activate_existing(record: Path, *, view="library", timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            saved = read_json(record)
            port = int(saved["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Invalid port")
            origin = f"http://127.0.0.1:{port}"
            response = httpx.post(origin + "/api/desktop/activate", json={"token": saved["token"], "view": view},
                                  headers={"Origin": origin}, timeout=2, trust_env=False)
            if response.status_code == 200 and response.json().get("ok"):
                return
        except (OSError, KeyError, ValueError, httpx.HTTPError):
            pass
        time.sleep(.2)
    raise RuntimeError("GeoSorter is already running but did not respond. Wait for startup to finish, then try again.")


def bundled_tools():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    tools = base / "tools"
    if tools.is_dir():
        # Only this process and its children inherit the bundled search path.
        paths = [tools / "exiftool", tools / "ffmpeg" / "bin"]
        os.environ["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + os.environ.get("PATH", "")


def hide_child_consoles():
    """Windowless desktop children, including existing ffmpeg/ExifTool callers."""
    import subprocess
    if os.name == "nt" and not getattr(subprocess.Popen, "_geosorter_hidden", False):
        original = subprocess.Popen
        class HiddenPopen(original):
            _geosorter_hidden = True
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
                super().__init__(*args, **kwargs)
        subprocess.Popen = HiddenPopen


def run(config_path=None, *, settings=False, state_dir=None, no_browser=False):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import pystray
    from PIL import Image, ImageDraw
    from .desktop import DesktopController, create_desktop_app

    root = tk.Tk()
    root.withdraw()
    state = Path(state_dir) if state_dir else config.default_data_dir() / "desktop"
    state.mkdir(parents=True, exist_ok=True)
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "geosorter.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("geosorter").addHandler(handler)
    logging.getLogger("geosorter").setLevel(logging.INFO)
    lock = InstanceLock(state)
    record = state / "instance.json"
    server = None
    server_thread = None
    tray = None
    sock = None
    owned = False
    commands = queue.Queue()

    def on_main(callback):
        future = Future()
        commands.put((callback, future))
        return future

    def pump():
        while not commands.empty():
            callback, future = commands.get_nowait()
            try:
                future.set_result(callback())
            except Exception as exc:
                future.set_exception(exc)
        if root.winfo_exists():
            root.after(50, pump)

    def choose(kind):
        def dialog():
            root.attributes("-topmost", True)
            try:
                if kind == "config":
                    return filedialog.askopenfilename(parent=root, title="Choose GeoSorter configuration", filetypes=[("GeoSorter configuration", "*.toml")]) or None
                if kind == "tool":
                    return filedialog.askopenfilename(parent=root, title="Choose tool", filetypes=[("Programs", "*.exe")]) or None
                return filedialog.askdirectory(parent=root, title="Choose folder", mustexist=True) or None
            finally:
                root.attributes("-topmost", False)
        return on_main(dialog).result()

    try:
        owned = lock.acquire()
        if not owned:
            activate_existing(record, view="settings" if settings else "library")
            return
        record.unlink(missing_ok=True)
        bundled_tools()
        hide_child_consoles()
        sock = listening_socket()
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"

        def open_browser(view="library"):
            if no_browser:
                return
            url = origin + "/#desktop=" + controller.secret + ("&settings=1" if view == "settings" else "")
            if not webbrowser.open(url):
                on_main(lambda: messagebox.showerror("Open GeoSorter", "Your browser could not be opened. Set a default browser in Windows Settings, then use Open GeoSorter in the tray.", parent=root))

        def stop_server():
            server.should_exit = True

        def startup_error(title, message):
            if messagebox.askyesno(title, message + "\n\nOpen logs for details?", parent=root):
                os.startfile(logs)

        controller = DesktopController(config_path, state_dir=state, choose_path=choose,
                                       on_quit=stop_server, on_activate=open_browser)
        app = create_desktop_app(controller, origin=origin)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
        server_thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=False)
        server_thread.start()
        save_json(record, {"port": port, "token": controller.secret, "pid": os.getpid()})

        def quit_tray():
            def decide():
                if controller.active() and not messagebox.askyesno("GeoSorter is working", "Quit when current work finishes? GeoSorter will keep running until then.", parent=root):
                    return
                controller.request_quit(wait=True)
            on_main(decide)

        icon = Image.new("RGBA", (64, 64), (15, 17, 22, 255))
        draw = ImageDraw.Draw(icon)
        draw.ellipse((13, 7, 51, 45), fill="#5eead4")
        draw.polygon([(17, 34), (47, 34), (32, 59)], fill="#5eead4")
        draw.ellipse((25, 19, 39, 33), fill="#0f1116")
        tray = pystray.Icon("GeoSorter", icon, "GeoSorter", menu=pystray.Menu(
            pystray.MenuItem("Open GeoSorter", lambda: open_browser(), default=True),
            pystray.MenuItem("Settings & Help", lambda: open_browser("settings")),
            pystray.MenuItem("Quit", quit_tray)))
        threading.Thread(target=tray.run, daemon=True).start()
        started = time.monotonic()

        def monitor():
            if not server_thread.is_alive():
                if not controller.closed:
                    startup_error("GeoSorter stopped", "GeoSorter stopped unexpectedly. Open it again to retry.")
                root.quit()
                return
            root.after(250, monitor)

        def wait_ready():
            if server.started:
                open_browser("settings" if settings else "library")
                monitor()
            elif not server_thread.is_alive() or time.monotonic() - started > 45:
                startup_error("GeoSorter could not start", "Startup did not complete. Check the logs, then try again.")
                controller.request_quit(wait=True)
                monitor()
            else:
                root.after(100, wait_ready)
        root.after(50, pump)
        root.after(100, wait_ready)
        root.mainloop()
    except Exception:
        logging.getLogger("geosorter").exception("Desktop launcher failed")
        if messagebox.askyesno("GeoSorter could not start", "GeoSorter could not start. Open the local logs for details?", parent=root):
            os.startfile(logs)
    finally:
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join()
        if tray:
            tray.stop()
        if sock:
            sock.close()
        if owned:
            record.unlink(missing_ok=True)
            lock.close()
        handler.close()
        logging.getLogger("geosorter").removeHandler(handler)
        root.destroy()


def main():
    if "--smoke-test" in sys.argv:
        bundled_tools()
        hide_child_consoles()
        import tkinter
        tkinter.Tcl().eval("info patchlevel")
        from .desktop import dependency_checks
        results = dependency_checks()
        from .repair import _download_context
        try:
            context = _download_context()
            results.append({"name": "download certificates", "ok": bool(context.get_ca_certs()), "message": "Ready"})
        except (OSError, ValueError) as exc:
            results.append({"name": "download certificates", "ok": False, "message": str(exc)})
        output = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
        output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        raise SystemExit(0 if all(check["ok"] for check in results) else 1)
    import argparse
    parser = argparse.ArgumentParser(description="GeoSorter Windows desktop")
    parser.add_argument("--config")
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    run(args.config, settings=args.settings, state_dir=args.state_dir, no_browser=args.no_browser)
