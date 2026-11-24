"""Create the Render web service and database for the lead bot.

Usage:
    RENDER_API_KEY=... python scripts/render_deploy.py

The API key is read from the process environment only. It is never stored in
this repository and never printed. Secrets from .env are sent directly to
Render as service environment variables.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.render.com/v1"
ROOT = Path(__file__).resolve().parent.parent
SERVICE_NAME = "vkparsingbot"
DB_NAME = "freelancer-pg"


class RenderError(RuntimeError):
    pass


def request(
    method: str,
    path: str,
    key: str,
    payload: dict[str, Any] | list[Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        raise RenderError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc


def unwrap(items: Any) -> list[dict[str, Any]]:
    return [item.get("service", item) if isinstance(item, dict) else item for item in items or []]


def local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def session_b64() -> str | None:
    session = ROOT / "sessions" / "freelancer_user.session"
    if not session.exists():
        return None
    return base64.b64encode(session.read_bytes()).decode()


def main() -> int:
    key = os.environ.get("RENDER_API_KEY", "").strip()
    if not key:
        print("RENDER_API_KEY is not set", file=sys.stderr)
        return 2

    owners = unwrap(request("GET", "/owners?limit=20", key))
    owner_id = os.environ.get("RENDER_OWNER_ID") or (
        owners[0].get("id") if owners else None
    )
    if not owner_id:
        print("No Render workspace found", file=sys.stderr)
        return 2

    databases = unwrap(request("GET", "/postgres?limit=20", key))
    database = next((d for d in databases if d.get("name") == DB_NAME), None)
    if database is None:
        database = request(
            "POST",
            "/postgres",
            key,
            {
                "ownerId": owner_id,
                "name": DB_NAME,
                "plan": "free",
                "region": "oregon",
                "version": "16",
                "databaseName": "freelancer_bot",
            },
        ).get("postgres", {})
    database_id = database["id"]

    for _ in range(40):
        current = request("GET", f"/postgres/{database_id}", key).get("postgres", {})
        if current.get("status") == "available":
            break
        time.sleep(3)
    else:
        print("Postgres did not become available", file=sys.stderr)
        return 1

    info = request("GET", f"/postgres/{database_id}/connection-info", key)
    external = info.get("externalConnectionString") or info.get("connectionInfo", {}).get(
        "externalConnectionString"
    )
    if not external:
        print("No external Postgres connection string returned", file=sys.stderr)
        return 1
    database_url = external.replace("postgres://", "postgresql+psycopg://", 1)

    env_values = local_env()
    # Never hand deployment credentials to the service itself.
    for name in ("RENDER_API_KEY", "RENDER_OWNER_ID", "GITHUB_TOKEN"):
        env_values.pop(name, None)
    env_values.pop("DATABASE_URL", None)
    env_values["DATABASE_URL"] = database_url
    encoded_session = session_b64()
    if encoded_session:
        env_values["TELEGRAM_SESSION_FILE_B64"] = encoded_session
    env_vars = [{"key": k, "value": v} for k, v in sorted(env_values.items())]

    services = unwrap(request("GET", "/services?limit=20", key))
    service = next((s for s in services if s.get("name") == SERVICE_NAME), None)
    if service is None:
        service = request(
            "POST",
            "/services",
            key,
            {
                "type": "web_service",
                "name": SERVICE_NAME,
                "ownerId": owner_id,
                "repo": "https://github.com/Virtuoso3004/telegram-freelance-lead-bot-v2",
                "branch": "main",
                "autoDeployTrigger": "commit",
                "serviceDetails": {
                    "runtime": "docker",
                    "plan": "free",
                    "region": "oregon",
                    "healthCheckPath": "/health",
                    "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
                },
                "envVars": env_vars,
            },
        ).get("service", {})
    else:
        request(
            "PUT",
            f"/services/{service['id']}/env-vars",
            key,
            env_vars,
        )
        request("POST", f"/services/{service['id']}/deploys", key, {})

    details = service.get("serviceDetails", {}) if service else {}
    print("service:", SERVICE_NAME)
    print("dashboard:", f"https://dashboard.render.com/web/{service.get('id', '')}")
    print("url:", details.get("url", "pending"))
    print("note: the repository must contain Dockerfile and freelancer_bot/render_entry.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
