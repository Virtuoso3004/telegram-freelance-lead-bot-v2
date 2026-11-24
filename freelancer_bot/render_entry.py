"""Render-compatible entrypoint: restore the session, serve /health, run the bot.

Render's free web services spin down after 15 minutes without inbound traffic.
The health endpoint lets an external pinger keep the service awake; the bot
itself keeps running in the same process.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
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


def keep_service_awake() -> None:
    """Ping our own public health URL so the free instance never spins down.

    Render considers any inbound HTTP request activity, including one that the
    service makes to its own public domain. External schedulers are unreliable,
    so the running process keeps itself warm instead.
    """
    url = (
        os.environ.get("RENDER_EXTERNAL_URL", "https://vkparsingbot.onrender.com")
        .rstrip("/")
        + "/health"
    )
    try:
        interval = max(60.0, float(os.environ.get("RENDER_SELF_PING_INTERVAL_SECONDS", "300")))
    except ValueError:
        interval = 300.0
    delay = threading.Event()

    def loop() -> None:
        while not delay.wait(interval):
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    status = response.status
                    response.read(64)
                print(f"render.self_ping status={status}", flush=True)
            except urllib.error.HTTPError as exc:
                print(f"render.self_ping_failed http={exc.code}", flush=True)
            except Exception as exc:  # a failed ping must never stop the bot
                print(f"render.self_ping_failed {type(exc).__name__}", flush=True)

    threading.Thread(target=loop, name="render-self-ping", daemon=True).start()
    print(f"render.self_ping_started interval={interval:g}", flush=True)


def bootstrap_database() -> None:
    """Render starts from an empty database every time a new DB is created."""
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    # A restored dump already contains the seeded sources.
    if not Path("/etc/secrets/db_dump.b64").exists():
        subprocess.run(
            [sys.executable, "-m", "freelancer_bot.persistence.source_seed"],
            check=True,
        )


def log_database_counts() -> None:
    """Emit restored row counts so a deploy can be verified from Render logs."""
    import psycopg

    url = os.environ.get("DATABASE_URL", "").replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    if not url:
        return
    tables = {
        "sources": "sources",
        "opportunities": "opportunities",
        "personalized_deliveries": "personalized_deliveries",
        "chat_discovery_peers": "telegram_chat_discovery_peers",
        "restore_state": "render_data_restore_state",
    }
    with psycopg.connect(url, connect_timeout=10) as connection:
        counts = {
            name: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for name, table in tables.items()
        }
        cursor = connection.execute(
            """SELECT decision_code, hard_filter_reasons, combined_relevance_score,
                      final_rank_score, minimum_relevance_threshold,
                      minimum_rank_score_threshold
               FROM match_traces ORDER BY evaluated_at DESC LIMIT 5"""
        )
        columns = [column.name for column in cursor.description]
        traces = [dict(zip(columns, row)) for row in cursor.fetchall()]
    print("render.db_counts " + json.dumps(counts, sort_keys=True), flush=True)
    for row in traces:
        print(
            "render.match_trace " + json.dumps(row, default=str, sort_keys=True),
            flush=True,
        )


def main() -> None:
    restore_user_session()
    serve_health()
    keep_service_awake()
    bootstrap_database()
    from .render_data_restore import restore_from_secret

    restore_from_secret(os.environ.get("DATABASE_URL", ""))
    try:
        log_database_counts()
    except Exception as exc:  # diagnostics must never block bot startup
        print(f"render.db_counts failed: {type(exc).__name__}", flush=True)
    from .app import cli

    sys.argv = ["freelancer_bot", "--run"]
    cli()


if __name__ == "__main__":
    main()
