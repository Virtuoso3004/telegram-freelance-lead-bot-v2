"""One-time restore of the local PGlite dump on Render.

The dump is uploaded as a base64 secret file because Render secret files must
be plaintext and are capped at 1 MB.  The service runs this after migrations
and before the bot starts, using the internal database connection, so no
external SSL path from the operator's machine is involved.

Rows are inserted with SQL instead of COPY: the local PGlite socket server
does not implement CopyFrom, so using INSERT keeps the dump verifiable locally.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import lzma
import os
import tarfile
from pathlib import Path

import psycopg
from psycopg import sql

LOGGER = logging.getLogger("freelancer_bot")

SECRET_PATH = Path("/etc/secrets/db_dump.b64")
RESTORE_KEY = "local-pglite-dump"
NULL_MARKER = r"\N"

TABLES_SQL = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition
ORDER BY 1
"""

FKS_SQL = """
SELECT child.relname, parent.relname
FROM pg_constraint con
JOIN pg_class child ON child.oid = con.conrelid
JOIN pg_class parent ON parent.oid = con.confrelid
JOIN pg_namespace n ON n.oid = child.relnamespace
WHERE con.contype = 'f' AND n.nspname = 'public'
  AND child.relname <> parent.relname
"""


def quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _ordered_tables(conn: psycopg.Connection) -> list[str]:
    tables = [row[0] for row in conn.execute(TABLES_SQL).fetchall()]
    parents: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in conn.execute(FKS_SQL).fetchall():
        if child in parents and parent in parents:
            parents[child].add(parent)

    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if not (parents[t] & remaining))
        if not ready:
            ready = sorted(remaining)
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _restoration_needed(conn: psycopg.Connection) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS render_data_restore_state (
               key text PRIMARY KEY,
               restored_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    return (
        conn.execute(
            "SELECT 1 FROM render_data_restore_state WHERE key = %s", (RESTORE_KEY,)
        ).fetchone()
        is None
    )


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s
                 AND is_generated = 'NEVER'
               ORDER BY ordinal_position""",
            (table,),
        ).fetchall()
    ]


def _read_rows(stream: io.BufferedIOBase, columns: list[str]) -> list[tuple[object, ...]]:
    text = io.StringIO(stream.read().decode("utf-8", errors="strict"))
    reader = csv.reader(text)
    next(reader, None)  # header
    rows: list[tuple[object, ...]] = []
    for values in reader:
        if len(values) != len(columns):
            raise ValueError(
                f"dump row has {len(values)} fields, expected {len(columns)}"
            )
        rows.append(
            tuple(None if value == NULL_MARKER else value for value in values)
        )
    return rows


def _advance_sequences(conn: psycopg.Connection, tables: list[str]) -> None:
    with conn.cursor() as cur:
        for table in tables:
            has_id = cur.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s
                     AND column_name = 'id'
                     AND data_type IN ('integer', 'bigint', 'smallint')""",
                (table,),
            ).fetchone()
            if not has_id:
                continue
            seq = cur.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')", (f"public.{table}",)
            ).fetchone()[0]
            if not seq:
                continue
            cur.execute(
                sql.SQL("SELECT setval(%s::regclass, COALESCE(MAX(id), 1), true) FROM {}").format(
                    sql.Identifier("public", table)
                ),
                (seq,),
            )


def restore_from_secret(database_url: str) -> bool:
    """Return True when the dump was restored, False when it was skipped."""
    if not SECRET_PATH.exists():
        return False
    if os.environ.get("RENDER_DATA_RESTORE_ENABLED", "true").lower() in {
        "0",
        "false",
        "no",
    }:
        return False

    encoded = SECRET_PATH.read_text(errors="strict").strip()
    raw = base64.b64decode(encoded, validate=True)
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        if not _restoration_needed(conn):
            LOGGER.info("render.data_restore.skipped", extra={"reason": "already_restored"})
            return False

        with tarfile.open(fileobj=io.BytesIO(lzma.decompress(raw)), mode="r:") as archive:
            members = {
                Path(member.name).name: member
                for member in archive.getmembers()
                if member.isfile()
            }
            ordered = [
                table for table in _ordered_tables(conn) if f"{table}.csv" in members
            ]

            # Clear children before parents so foreign keys stay valid.
            with conn.cursor() as cur:
                for table in reversed(ordered):
                    cur.execute(
                        sql.SQL("DELETE FROM {}").format(sql.Identifier("public", table))
                    )

            for table in ordered:
                stream = archive.extractfile(members[f"{table}.csv"])
                if stream is None:
                    continue
                columns = _columns(conn, table)
                rows = _read_rows(stream, columns)
                if not rows:
                    continue
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier("public", table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                with conn.cursor() as cur:
                    cur.executemany(statement, rows)
                LOGGER.info(
                    "render.data_restore.table",
                    extra={"table": table, "rows": len(rows)},
                )

        _advance_sequences(conn, ordered)
        conn.execute(
            "INSERT INTO render_data_restore_state (key) VALUES (%s)", (RESTORE_KEY,)
        )
        conn.commit()
    LOGGER.info("render.data_restore.completed")
    return True
