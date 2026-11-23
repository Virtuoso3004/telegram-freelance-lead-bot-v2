#!/usr/bin/env bash
set -u

ROOT=/root/telegram-freelance-lead-bot-v2
DB_DIR="$ROOT/.local_db"
LOG="$ROOT/local_runtime.log"
LOCK="$ROOT/.local_supervisor.lock"
UV=/root/.local/uv-0.12.2/uv
PG_URL='postgresql://postgres:postgres@127.0.0.1:5432/postgres'

# Keep the CPU awake while the screen is off. This does not survive a force
# close of Termux or a reboot — it only prevents Android from suspending us.
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  trap 'termux-wake-unlock' EXIT INT TERM
fi

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$ROOT"

start_db() {
  if ! pgrep -f "node $DB_DIR/server.mjs" >/dev/null 2>&1; then
    setsid node "$DB_DIR/server.mjs" >>"$LOG" 2>&1 </dev/null &
  fi
}

wait_db() {
  local i
  for i in $(seq 1 30); do
    if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cleanup_runtime_state() {
  psql "$PG_URL" -q -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL' || true
UPDATE telegram_collector_operation_state
SET status = 'ready',
    active_request_token = NULL,
    active_request_category = NULL,
    active_request_started_at = NULL,
    active_request_lease_until = NULL,
    cooldown_until = NULL,
    updated_at = now()
WHERE active_request_token IS NOT NULL;

UPDATE discovery_runs
SET status = 'failed',
    failure_code = 'interrupted_by_restart',
    finished_at = now()
WHERE status = 'running';

DELETE FROM discovery_runs
WHERE failure_code = 'interrupted_by_restart';

UPDATE durable_jobs
SET state = 'queued',
    claimed_at = NULL,
    lease_owner = NULL,
    lease_expires_at = NULL,
    available_at = now(),
    updated_at = now()
WHERE state = 'running';
SQL
}

while true; do
  start_db
  if wait_db; then
    cleanup_runtime_state
    "$UV" run --frozen python -m freelancer_bot --run >>"$LOG" 2>&1
  fi
  sleep 3
done
