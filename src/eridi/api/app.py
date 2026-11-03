"""The local web app: FastAPI on 127.0.0.1, serving JSON under /api and the UI in /ui.

Start it with `eridi open`. See `security.py` for how requests are checked.
"""

import contextlib
import json
import logging
import os
import socket
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from eridi import __version__
from eridi.api.google import GoogleSignIn
from eridi.api.microsoft import MicrosoftSignIn
from eridi.api.routes import router
from eridi.api.scanner import BackgroundScanner
from eridi.api.security import COOKIE_NAME, Guard, get_or_create_token
from eridi.core.paths import app_home
from eridi.core.service import Eridi
from eridi.core.watch import Watcher

log = logging.getLogger(__name__)

SERVER_FILE = "server.json"
HEALTH_PATH = "/api/health"
SIGNED_IN_PAGE = (
    '<!doctype html><meta charset="utf-8"><title>Eridi</title>'
    '<script src="/ui/signed-in.js"></script>'
    '<noscript><meta http-equiv="refresh" content="0; url=/"></noscript>'
)
UI_FILES = {"app.js": "text/javascript", "app.css": "text/css", "signed-in.js": "text/javascript"}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def ui_dir() -> Path:
    return Path(str(resources.files("eridi") / "ui"))


def create_app(
    eridi: Eridi,
    *,
    token: str,
    port: int,
    scanner: BackgroundScanner | None = None,
    on_ready: Callable[[Guard], None] | None = None,
    watch: bool | None = None,
) -> FastAPI:
    """`watch=None` follows `settings.watch`."""
    guard = Guard(token, port)
    scanner = scanner or BackgroundScanner(eridi, eridi.config.settings.scan_interval_minutes)
    watcher = Watcher(eridi) if (eridi.config.settings.watch if watch is None else watch) else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scanner.start()
        if watcher is not None:
            watcher.start()
        if on_ready is not None:
            on_ready(guard)
        try:
            yield
        finally:
            if watcher is not None:
                watcher.stop()
            scanner.stop()

    app = FastAPI(title="Eridi", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.eridi = eridi
    app.state.guard = guard
    app.state.scanner = scanner
    app.state.microsoft = MicrosoftSignIn(eridi)
    app.state.google = GoogleSignIn(eridi)
    app.include_router(router, prefix="/api")

    @app.middleware("http")
    async def protect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not guard.host_ok(request.headers.get("host")):
            return PlainTextResponse("Unexpected Host header.", status_code=400)
        if request.url.path == HEALTH_PATH and request.method == "GET":
            # No token needed: says only that this is Eridi and which version. Used by the
            # desktop app and by release smoke tests.
            return JSONResponse({"ok": True, "app": "eridi", "version": __version__},
                                headers={"Cache-Control": "no-store"})  # fmt: skip
        if not guard.origin_ok(
            request.headers.get("origin"), request.headers.get("sec-fetch-site")
        ):
            return PlainTextResponse("Cross-origin requests are not allowed.", status_code=403)
        link = request.query_params.get("t")
        if request.url.path == "/" and link is not None:
            if not guard.redeem(link):
                return PlainTextResponse(
                    "This link has expired. Run `eridi open` again.", status_code=401
                )
            # Not a redirect: when the link is opened from another site (the desktop app's
            # window starts on its own page), browsers drop a SameSite=Strict cookie set on
            # a redirect. A page on this site that moves on by itself keeps it.
            response: Response = HTMLResponse(SIGNED_IN_PAGE)
            response.set_cookie(
                COOKIE_NAME, guard.session_value, httponly=True, samesite="strict", path="/"
            )
        elif not guard.authorized(
            request.headers.get("authorization"), request.cookies.get(COOKIE_NAME)
        ):
            return JSONResponse(
                {"error": "Not signed in. Open Eridi with `eridi open`."}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ui_dir() / "index.html", media_type="text/html")

    @app.get("/ui/{name}", include_in_schema=False)
    def ui_file(name: str) -> Response:
        if name not in UI_FILES:
            return PlainTextResponse("Not found.", status_code=404)
        return FileResponse(ui_dir() / name, media_type=UI_FILES[name])

    return app


def server_file() -> Path:
    return app_home() / SERVER_FILE


def free_socket() -> socket.socket:
    """A socket bound to a free port on 127.0.0.1 only."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def serve(
    eridi: Eridi,
    *,
    open_browser: bool = True,
    announce: Callable[[str], None] = print,
    sidecar: bool = False,
) -> None:
    """Run the app until interrupted. Writes `<app-data>/server.json` while running.

    `sidecar=True` is for the desktop app, which starts the engine and talks to it over
    stdin and stdout, one JSON object per line:
    - when ready, the engine prints `{"event": "ready", "port", "url", "version"}`, where
      `url` is a one-time sign-in link for the app's window;
    - the app writes `link` to get `{"event": "link", "url"}` (for a new window), or `scan`
      to scan now;
    - when the app closes stdin (it quit, or crashed), the engine stops.
    """
    import uvicorn

    sock = free_socket()
    port = sock.getsockname()[1]
    scanner = BackgroundScanner(eridi, eridi.config.settings.scan_interval_minutes)
    server: uvicorn.Server | None = None

    def ready(guard: Guard) -> None:
        url = f"http://127.0.0.1:{port}/?t={guard.new_link()}"
        server_file().write_text(
            json.dumps({"port": port, "pid": os.getpid()}) + "\n", encoding="utf-8"
        )
        if sidecar:
            _emit({"event": "ready", "port": port, "url": url, "version": __version__})
            threading.Thread(target=_sidecar_commands, args=(guard, port, scanner, stop),
                             name="eridi-sidecar", daemon=True).start()  # fmt: skip
            return
        announce(f"Eridi is running at http://127.0.0.1:{port} (Ctrl+C to stop).")
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        else:
            announce(f"Open: {url}")

    def stop() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(eridi, token=get_or_create_token(), port=port, scanner=scanner, on_ready=ready)
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        server_file().unlink(missing_ok=True)


def _emit(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _sidecar_commands(
    guard: Guard, port: int, scanner: BackgroundScanner, stop: Callable[[], None]
) -> None:
    """Answer the desktop app's commands on stdin; stop when it closes stdin."""
    for line in sys.stdin:
        command = line.strip()
        if command == "link":
            _emit({"event": "link", "url": f"http://127.0.0.1:{port}/?t={guard.new_link()}"})
        elif command == "scan":
            _emit({"event": "scan", "started": scanner.request()})
        elif command == "quit":
            break
    stop()
