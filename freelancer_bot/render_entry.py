"""Render-compatible entrypoint: restore the session, serve /health, run the bot.

Render's free web services spin down after 15 minutes without inbound traffic.
The health endpoint lets an external pinger keep the service awake; the bot
itself keeps running in the same process.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def restore_user_session() -> Path | None:
    encoded = os.environ.get("TELEGRAM_SESSION_FILE_B64", "").strip()
    if not encoded:
        return None
    configured = Path(os.environ.get("USER_SESSION_PATH", "sessions/freelancer_user"))
    target = configured if configured.suffix else configured.with_suffix(".session")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded, validate=True))
    target.chmod(0o600)
    return target


def serve_health() -> ThreadingHTTPServer:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, name="render-health", daemon=True).start()
    return server


def bootstrap_database() -> None:
    """Render starts from an empty database every time a new DB is created."""
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    subprocess.run(
        [sys.executable, "-m", "freelancer_bot.persistence.source_seed"],
        check=True,
    )


def main() -> None:
    restore_user_session()
    serve_health()
    bootstrap_database()
    from .app import cli

    sys.argv = ["freelancer_bot", "--run"]
    cli()


if __name__ == "__main__":
    main()
