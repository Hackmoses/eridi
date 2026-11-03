"""A real uvicorn server on a free 127.0.0.1 port, in a background thread, for tests."""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from eridi.api.app import create_app, free_socket, server_file
from eridi.api.scanner import BackgroundScanner
from eridi.api.security import Guard, get_or_create_token
from eridi.core.service import Eridi


@contextmanager
def live_server(eridi: Eridi, *, watch: bool = False) -> Iterator[tuple[int, Guard]]:
    sock = free_socket()
    port = sock.getsockname()[1]
    app = create_app(
        eridi,
        token=get_or_create_token(),
        port=port,
        scanner=BackgroundScanner(eridi, 0),
        watch=watch,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    server_file().write_text(json.dumps({"port": port}), encoding="utf-8")
    try:
        yield port, app.state.guard
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        server_file().unlink(missing_ok=True)
