"""SQLite setup and data access.

Schema version 6 adds bot-to-bot networking: the ``requests`` table,
``conversations.request_id``, generic offering attributes
(``opportunities.category`` / ``attributes_json``), and owner conditions
(``twins.conditions_json``). Version 7 adds ``usage_log.status`` /
``usage_log.notes`` so failed model calls are recorded instead of
vanishing. Version 8 adds ``usage_log.latency_ms`` / ``usage_log.attempts``
for multi-provider telemetry. Version 9 adds ``memories.category`` /
``memories.source_time`` for universal knowledge import. Version 10
adds the project-execution tables (projects, project_tasks,
project_files, project_events). Version 11 adds the messaging
tables (channel_accounts, channel_contacts, outbox_messages,
channel_events). Version 12 adds revoked_sessions so logout
really invalidates sessions. All helpers accept an optional
``path`` so tests can use a temporary database file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from sqlite3 import Connection, Row
from typing import Any

from . import schema as app_schema
from .config import settings

SCHEMA_VERSION = 12


#: Columns added after the initial schema. Every entry's DDL must be
#: valid on both SQLite and PostgreSQL; both drivers apply this list.
_ENSURE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "password_hash", "TEXT"),
    ("twins", "is_public", "INTEGER NOT NULL DEFAULT 1"),
    ("twins", "conditions_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("opportunities", "category", "TEXT NOT NULL DEFAULT ''"),
    ("opportunities", "attributes_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("conversations", "request_id", "INTEGER"),
    ("usage_log", "status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("usage_log", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("usage_log", "latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_log", "attempts", "INTEGER NOT NULL DEFAULT 1"),
    ("memories", "category", "TEXT NOT NULL DEFAULT 'general'"),
    ("memories", "source_time", "TEXT"),
)


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing table if it is missing (idempotent)."""
    if driver_name(conn) == "postgres":
        existing = {
            r["name"]
            for r in conn.execute(
                "SELECT column_name AS name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            )
        }
    else:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def db_path() -> Path:
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _resolve(path: Path | str | None) -> Path:
    if path is None:
        return db_path()
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path(__file__).resolve().parent.parent / resolved
    return resolved


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _duplicate_errors():
    errors: list = [sqlite3.IntegrityError]
    try:
        from psycopg import errors as pg_errors

        errors.append(pg_errors.UniqueViolation)
    except ImportError:  # pragma: no cover - psycopg always installed
        pass
    return tuple(errors)


#: Unique-constraint violations on either driver. Callers must roll back
#: before reusing the connection (PostgreSQL aborts the whole transaction).
DUPLICATE_ERRORS = _duplicate_errors()


def _integrity_errors():
    errors: list = [sqlite3.IntegrityError]
    try:
        from psycopg import errors as pg_errors

        errors.append(pg_errors.IntegrityError)
    except ImportError:  # pragma: no cover - psycopg always installed
        pass
    return tuple(errors)


#: Any constraint violation (unique, foreign key, check, not null) on
#: either driver. Tests asserting constraint behavior use this.
INTEGRITY_ERRORS = _integrity_errors()


def driver_name(conn) -> str:
    """'postgres' for pooled connections, 'sqlite' otherwise."""
    return getattr(conn, "driver", "sqlite")


TEST_SCHEMA = "mira_test"


def test_database_url() -> str:
    """PostgreSQL URL for the test suite.

    Empty — or the explicit sentinel ``sqlite`` — means SQLite test files.
    """
    value = os.environ.get("MIRA_TEST_DATABASE_URL", "").strip()
    if value.lower() == "sqlite":
        return ""
    return value


def translate_pg(sql: str) -> str:
    """Rewrite SQLite-style placeholders for psycopg.

    ``?`` becomes ``%s`` and ``IS ?`` becomes ``IS NOT DISTINCT FROM %s``
    (PostgreSQL has no parameterized ``IS``). Single-quoted string
    literals are left untouched.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch != "?":
            out.append(ch)
            i += 1
            continue
        j = len(out) - 1
        while j >= 0 and out[j] in (" ", "\t", "\n"):
            j -= 1
        word = "".join(out[max(0, j - 1):j + 1]).upper()
        bare = j - 2 < 0 or not (out[j - 2].isalnum() or out[j - 2] == "_")
        if word == "IS" and bare:
            del out[j - 1:]
            out.append("IS NOT DISTINCT FROM %s")
        else:
            out.append("%s")
        i += 1
    return "".join(out)


_POOLS: dict[str, Any] = {}


def _pool(url: str):
    if url not in _POOLS:
        import atexit

        from psycopg_pool import ConnectionPool

        from psycopg.rows import dict_row

        pool = ConnectionPool(
            url, min_size=1, max_size=4, open=True,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            timeout=30)
        atexit.register(pool.close)
        _POOLS[url] = pool
    return _POOLS[url]


class PGConnection:
    """Minimal sqlite3-compatible wrapper over a pooled psycopg connection.

    Only the surface the codebase uses: ``execute`` (with ``?``
    placeholders), ``commit``, ``rollback``, ``close``. Rows are dicts.
    There is intentionally no ``lastrowid``: inserts must go through
    ``insert_returning_id`` so both drivers stay correct.
    """

    driver = "postgres"

    def __init__(self, raw, pool, schema: str | None = None):
        self._raw = raw
        self._pool = pool
        raw.execute("SET TIME ZONE 'UTC'")
        if schema:
            raw.execute(f"SET search_path TO {schema}, public")

    def execute(self, sql: str, params=()):
        return self._raw.execute(translate_pg(sql), params or ())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        # Match sqlite3 semantics: closing rolls back uncommitted work.
        # Without this, returned SELECT transactions keep their locks and
        # block schema DDL (and each other) indefinitely.
        try:
            from psycopg.pq import TransactionStatus

            if self._raw.info.transaction_status != TransactionStatus.IDLE:
                self._raw.rollback()
        finally:
            self._pool.putconn(self._raw)


def _pg_connection(url: str, schema: str | None = None) -> PGConnection:
    pool = _pool(url)
    return PGConnection(pool.getconn(), pool, schema)


def insert_returning_id(conn, sql: str, params=()) -> int:
    """Run one INSERT and return its new id on either driver."""
    if isinstance(conn, PGConnection):
        row = conn.execute(sql + " RETURNING id", params).fetchone()
        return int(row["id"])
    return conn.execute(sql, params).lastrowid


def get_connection(path: Path | str | None = None):
    test_url = test_database_url()
    if test_url:
        return _pg_connection(test_url, TEST_SCHEMA)
    if path is None and settings.database_url:
        return _pg_connection(settings.database_url)
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _init_postgres(url: str, schema: str | None) -> None:
    conn = _pg_connection(url, None)
    try:
        if schema:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.execute(f"SET search_path TO {schema}, public")
        for statement in app_schema.statements("postgres"):
            conn.execute(statement)
        for table, column, ddl in _ENSURE_COLUMNS:
            _ensure_column(conn, table, column, ddl)
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET version=excluded.version",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | str | None = None) -> Path:
    test_url = test_database_url()
    if test_url:
        _init_postgres(test_url, TEST_SCHEMA)
        return Path("postgres-test")
    if path is None and settings.database_url:
        _init_postgres(settings.database_url, None)
        return Path("postgres")
    target = _resolve(path)
    conn = get_connection(target)
    try:
        for statement in app_schema.statements("sqlite"):
            conn.execute(statement)
        for table, column, ddl in _ENSURE_COLUMNS:
            _ensure_column(conn, table, column, ddl)
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET version=excluded.version",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
    return target


def create_user(
    conn: Connection, *, name: str, role: str, email: str,
    password_hash: str | None = None,
) -> int:
    return insert_returning_id(
        conn,
        "INSERT INTO users (name, role, email, password_hash) VALUES (?, ?, ?, ?)",
        (name, role, email, password_hash),
    )


def get_user(conn: Connection, user_id: int) -> Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(conn: Connection, email: str) -> Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def set_user_password(conn: Connection, user_id: int, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def create_twin(
    conn: Connection,
    *,
    user_id: int,
    display_name: str,
    persona: str,
    tone: str,
    profile: dict[str, Any],
    authorized_fields: list,
    avatar_seed: str,
    is_public: bool = True,
) -> int:
    return insert_returning_id(
        conn,
        "INSERT INTO twins (user_id, display_name, persona, tone,"
        " profile_json, authorized_fields_json, avatar_seed, is_public)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            display_name,
            persona,
            tone,
            json.dumps(profile),
            json.dumps(authorized_fields),
            avatar_seed,
            1 if is_public else 0,
        ),
    )


def visible_twins(conn: Connection, viewer_user_id: int | None) -> list[Row]:
    """Twins visible to a viewer: public twins plus their own (any status)."""
    return list(
        conn.execute(
            "SELECT twins.*, users.name AS owner_name, users.role AS owner_role,"
            " users.email AS owner_email"
            " FROM twins JOIN users ON users.id = twins.user_id"
            " WHERE twins.is_public = 1 OR twins.user_id IS ?"
            " ORDER BY twins.id",
            (viewer_user_id,),
        )
    )


def update_twin(
    conn: Connection,
    twin_id: int,
    *,
    display_name: str,
    persona: str,
    tone: str,
    profile: dict[str, Any],
    authorized_fields: list,
    is_public: bool | None = None,
) -> None:
    if is_public is None:
        conn.execute(
            "UPDATE twins SET display_name = ?, persona = ?, tone = ?,"
            " profile_json = ?, authorized_fields_json = ? WHERE id = ?",
            (display_name, persona, tone, json.dumps(profile),
             json.dumps(authorized_fields), twin_id),
        )
    else:
        conn.execute(
            "UPDATE twins SET display_name = ?, persona = ?, tone = ?,"
            " profile_json = ?, authorized_fields_json = ?, is_public = ?"
            " WHERE id = ?",
            (display_name, persona, tone, json.dumps(profile),
             json.dumps(authorized_fields), 1 if is_public else 0, twin_id),
        )


def delete_twin(conn: Connection, twin_id: int) -> None:
    conn.execute("DELETE FROM twins WHERE id = ?", (twin_id,))


def get_twin_by_user(conn: Connection, user_id: int) -> Row | None:
    return conn.execute(
        "SELECT twins.*, users.name AS owner_name, users.role AS owner_role,"
        " users.email AS owner_email"
        " FROM twins JOIN users ON users.id = twins.user_id"
        " WHERE twins.user_id = ?",
        (user_id,),
    ).fetchone()


def list_twins(conn: Connection) -> list[Row]:
    return list(
        conn.execute(
            "SELECT twins.*, users.name AS owner_name, users.role AS owner_role,"
            " users.email AS owner_email"
            " FROM twins JOIN users ON users.id = twins.user_id"
            " ORDER BY twins.id"
        )
    )


def get_twin(conn: Connection, twin_id: int) -> Row | None:
    return conn.execute(
        "SELECT twins.*, users.name AS owner_name, users.role AS owner_role,"
        " users.email AS owner_email"
        " FROM twins JOIN users ON users.id = twins.user_id"
        " WHERE twins.id = ?",
        (twin_id,),
    ).fetchone()


_OPP_SELECT = (
    "SELECT opportunities.*, users.name AS owner_name, users.role AS owner_role,"
    " twins.display_name AS owner_twin_name,"
    " twins.avatar_seed AS owner_avatar_seed"
    " FROM opportunities JOIN users ON users.id = opportunities.owner_user_id"
    " LEFT JOIN twins ON twins.user_id = users.id"
)


def create_opportunity(
    conn: Connection,
    *,
    owner_user_id: int,
    kind: str = "job",
    title: str,
    description: str = "",
    required_skills: list | None = None,
    preferred_experience_years: int | None = None,
    location: str = "",
    remote_ok: bool = False,
    status: str = "open",
    category: str = "",
    attributes: dict | None = None,
) -> int:
    return insert_returning_id(
        conn,
        "INSERT INTO opportunities (owner_user_id, kind, title, description,"
        " required_skills_json, preferred_experience_years, location,"
        " remote_ok, status, category, attributes_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            owner_user_id,
            kind,
            title,
            description,
            json.dumps(required_skills or []),
            preferred_experience_years,
            location,
            1 if remote_ok else 0,
            status,
            category,
            json.dumps(attributes or {}),
        ),
    )


def get_opportunity(conn: Connection, opp_id: int) -> Row | None:
    return conn.execute(_OPP_SELECT + " WHERE opportunities.id = ?", (opp_id,)).fetchone()


def get_opportunity_by_owner_title(
    conn: Connection, owner_user_id: int, title: str
) -> Row | None:
    return conn.execute(
        _OPP_SELECT + " WHERE opportunities.owner_user_id = ? AND opportunities.title = ?",
        (owner_user_id, title),
    ).fetchone()


def list_opportunities(
    conn: Connection, *, status: str | None = "open", kind: str | None = None,
    category: str | None = None, limit: int = 500,
) -> list[Row]:
    query = _OPP_SELECT
    params: list[Any] = []
    clauses = []
    if status is not None and status != "all":
        clauses.append("opportunities.status = ?")
        params.append(status)
    if kind is not None:
        clauses.append("opportunities.kind = ?")
        params.append(kind)
    if category:
        clauses.append("opportunities.category = ?")
        params.append(category)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY opportunities.id LIMIT ?"
    params.append(max(1, min(2000, limit)))
    return list(conn.execute(query, params))


def set_twin_conditions(conn: Connection, twin_id: int, conditions: dict) -> None:
    conn.execute("UPDATE twins SET conditions_json = ? WHERE id = ?",
                 (json.dumps(conditions), twin_id))


def create_request(
    conn: Connection,
    *,
    twin_id: int,
    owner_user_id: int,
    category: str = "other",
    need_text: str = "",
    requirements: dict | None = None,
    conditions: dict | None = None,
    idempotency_key: str,
) -> int:
    return insert_returning_id(
        conn,
        "INSERT INTO requests (twin_id, owner_user_id, category, need_text,"
        " requirements_json, conditions_json, status, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
        (twin_id, owner_user_id, category, need_text,
         json.dumps(requirements or {}), json.dumps(conditions or {}),
         idempotency_key),
    )


def get_request(conn: Connection, req_id: int) -> Row | None:
    return conn.execute(
        "SELECT requests.*, twins.display_name AS twin_name,"
        " users.name AS owner_name FROM requests"
        " JOIN twins ON twins.id = requests.twin_id"
        " JOIN users ON users.id = requests.owner_user_id"
        " WHERE requests.id = ?",
        (req_id,),
    ).fetchone()


def get_request_by_key(conn: Connection, owner_user_id: int, key: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM requests WHERE owner_user_id = ? AND idempotency_key = ?",
        (owner_user_id, key),
    ).fetchone()


def list_requests_for_user(conn: Connection, user_id: int) -> list[Row]:
    return list(conn.execute(
        "SELECT requests.*, twins.display_name AS twin_name FROM requests"
        " JOIN twins ON twins.id = requests.twin_id"
        " WHERE requests.owner_user_id = ?"
        " ORDER BY requests.updated_at DESC, requests.id DESC",
        (user_id,),
    ))


def update_request(
    conn: Connection, req_id: int, *, category: str | None = None,
    requirements: dict | None = None, conditions: dict | None = None,
    status: str | None = None,
) -> None:
    sets, params = [], []
    if category is not None:
        sets.append("category = ?")
        params.append(category)
    if requirements is not None:
        sets.append("requirements_json = ?")
        params.append(json.dumps(requirements))
    if conditions is not None:
        sets.append("conditions_json = ?")
        params.append(json.dumps(conditions))
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_utc_stamp())
    params.append(req_id)
    conn.execute(f"UPDATE requests SET {', '.join(sets)} WHERE id = ?", params)
