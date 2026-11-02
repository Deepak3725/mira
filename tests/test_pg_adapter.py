"""Deploy-prep tests: PG adapter translation, schema parity, dual driver."""

import os
import tempfile
import unittest
from pathlib import Path

from app import schema as app_schema
from app.db import (
    SCHEMA_VERSION,
    get_connection,
    init_db,
    insert_returning_id,
    translate_pg,
)


class TestTranslatePg(unittest.TestCase):
    def test_placeholders(self):
        self.assertEqual(
            translate_pg("SELECT * FROM users WHERE id = ? AND email = ?"),
            "SELECT * FROM users WHERE id = %s AND email = %s")

    def test_is_none_comparison(self):
        self.assertEqual(
            translate_pg("user_id IS ? AND conv_id IS ?"),
            "user_id IS NOT DISTINCT FROM %s AND conv_id IS NOT DISTINCT FROM %s")

    def test_string_literals_untouched(self):
        self.assertEqual(
            translate_pg("SELECT 'what?' AS q WHERE a = ?"),
            "SELECT 'what?' AS q WHERE a = %s")

    def test_escaped_quote_inside_literal(self):
        self.assertEqual(
            translate_pg("SELECT 'it''s ? ok' WHERE a = ?"),
            "SELECT 'it''s ? ok' WHERE a = %s")

    def test_is_inside_word_not_rewritten(self):
        self.assertEqual(
            translate_pg("WHERE analysis = ?"),
            "WHERE analysis = %s")

    def test_no_placeholders_unchanged(self):
        sql = "SELECT COUNT(*) AS n FROM messages"
        self.assertEqual(translate_pg(sql), sql)


class TestSchemaParity(unittest.TestCase):
    TABLES = {"schema_version", "users", "twins", "opportunities",
              "requests", "conversations", "messages", "audit_events",
              "usage_log", "memories", "twin_chats"}

    def test_postgres_has_no_sqlite_idioms(self):
        blob = "\n".join(app_schema.statements("postgres"))
        for table in self.TABLES:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", blob)
        self.assertNotIn("AUTOINCREMENT", blob)
        self.assertNotIn("datetime(", blob)
        self.assertNotIn("PRAGMA", blob)
        self.assertIn("GENERATED ALWAYS AS IDENTITY", blob)
        self.assertIn("timestamptz", blob)

    def test_postgres_references_resolve_in_order(self):
        created = set()
        for stmt in app_schema.statements("postgres"):
            if stmt.startswith("CREATE TABLE"):
                created.add(stmt.split()[5])
            for ref in ("users(id)", "twins(id)", "opportunities(id)",
                        "requests(id)", "conversations(id)"):
                if f"REFERENCES {ref}" in stmt:
                    self.assertIn(ref.split("(")[0], created)

    def test_unknown_driver_rejected(self):
        with self.assertRaises(ValueError):
            app_schema.statements("mysql")

    def test_sqlite_init_matches_schema_module(self):
        # Fresh SQLite databases must contain every table/column the
        # single-source schema declares (guards init_db/schema.py drift).
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "sync.db")
            init_db(db)
            conn = get_connection(db)
            try:
                from app.db import driver_name as _driver

                for name, columns, _ in app_schema._TABLES:
                    if _driver(conn) == "postgres":
                        info = conn.execute(
                            "SELECT column_name AS name"
                            " FROM information_schema.columns"
                            " WHERE table_schema = current_schema()"
                            " AND table_name = ?", (name,)).fetchall()
                    else:
                        info = conn.execute(
                            f"PRAGMA table_info({name})").fetchall()
                    have = {r["name"] for r in info}
                    for col in columns:
                        col = (col.replace("{ID}", "id")
                                  .replace("{TS_CREATED}", "created_at")
                                  .replace("{TS_UPDATED}", "updated_at")
                                  .replace("{REAL}", "REAL"))
                        self.assertIn(col.split()[0], have)
                version = conn.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()["version"]
                self.assertEqual(version, SCHEMA_VERSION)
            finally:
                conn.close()


class TestInitPostgresSafety(unittest.TestCase):
    def test_production_path_never_drops_and_commits(self):
        from unittest import mock

        from app import db as db_mod

        calls = []

        class FakeConn:
            driver = "postgres"

            def execute(self, sql, params=()):
                calls.append(sql)
                return []

            def commit(self):
                calls.append("COMMIT")

            def close(self):
                calls.append("CLOSE")

        with mock.patch.object(db_mod, "_pg_connection",
                               return_value=FakeConn()):
            db_mod._init_postgres("postgres://prod/db", None)
        self.assertTrue(any("CREATE TABLE" in c for c in calls))
        self.assertFalse(any("DROP SCHEMA" in c for c in calls))
        self.assertIn("COMMIT", calls)
        self.assertEqual(calls[-1], "CLOSE")

    def test_test_schema_drop_is_scoped_to_test_schema(self):
        from unittest import mock

        from app import db as db_mod

        calls = []

        class FakeConn:
            driver = "postgres"

            def execute(self, sql, params=()):
                calls.append(sql)
                return []

            def commit(self):
                calls.append("COMMIT")

            def close(self):
                calls.append("CLOSE")

        with mock.patch.object(db_mod, "_pg_connection",
                               return_value=FakeConn()):
            db_mod._init_postgres("postgres://test/db",
                                  db_mod.TEST_SCHEMA)
        drops = [c for c in calls if "DROP SCHEMA" in c]
        self.assertEqual(len(drops), 1)
        self.assertIn(db_mod.TEST_SCHEMA, drops[0])
        self.assertIn("COMMIT", calls)


class TestDatabaseRedirectGuard(unittest.TestCase):
    """MIRA_TEST_DATABASE_URL must never redirect the app itself.

    The variable leaked from .env into the running server: login read
    the shared Postgres test schema (freshly wiped by boot-time
    init_db) instead of data/mira.db, and every DB-backed page hung
    then 500'd whenever Postgres was unreachable. The redirect now
    applies only when the test-suite marker is active.
    """

    URL = "postgresql://user:pw@localhost:5432/mira_test"
    MODE_VAR = "MIRA_TEST_MODE"
    URL_VAR = "MIRA_TEST_DATABASE_URL"

    def setUp(self):
        from app import db as db_mod

        self.db_mod = db_mod
        self._saved = {k: os.environ.get(k)
                       for k in (self.MODE_VAR, self.URL_VAR)}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_app_context_ignores_test_url(self):
        os.environ.pop(self.MODE_VAR, None)
        os.environ[self.URL_VAR] = self.URL
        self.assertEqual(self.db_mod.test_database_url(), "")

    def test_app_context_uses_sqlite_path_despite_test_url(self):
        import sqlite3

        os.environ.pop(self.MODE_VAR, None)
        os.environ[self.URL_VAR] = self.URL
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "app.db")
            init_db(db)
            conn = get_connection(db)
            try:
                self.assertIsInstance(conn, sqlite3.Connection)
                conn.execute(
                    "INSERT INTO users (name, role, email)"
                    " VALUES ('App', 'professional', 'app@example.com')")
                row = conn.execute(
                    "SELECT email FROM users"
                    " WHERE email = 'app@example.com'").fetchone()
                self.assertEqual(row["email"], "app@example.com")
            finally:
                conn.close()

    def test_test_mode_still_honors_test_url(self):
        os.environ[self.MODE_VAR] = "1"
        os.environ[self.URL_VAR] = self.URL
        self.assertEqual(self.db_mod.test_database_url(), self.URL)

    def test_sqlite_sentinel_stays_empty_in_test_mode(self):
        os.environ[self.MODE_VAR] = "1"
        os.environ[self.URL_VAR] = "sqlite"
        self.assertEqual(self.db_mod.test_database_url(), "")


class TestV12ToV13Migration(unittest.TestCase):
    def test_existing_v12_database_migrates(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "v12.db")
            init_db(db)
            conn = get_connection(db)
            try:
                conn.execute(
                    "INSERT INTO users (name, role, email)"
                    " VALUES ('Old', 'professional', 'old@example.com')")
                conn.commit()
                # Rewind to a v12 shape: no match tables, no new
                # request columns, no v13 indexes, version stamped 12.
                for index in ("idx_req_expires", "idx_req_twin",
                              "idx_match_search", "idx_match_counter",
                              "idx_notif_user", "idx_mq_status"):
                    conn.execute(f"DROP INDEX IF EXISTS {index}")
                conn.execute("DROP TABLE matches")
                conn.execute("DROP TABLE notifications")
                conn.execute("DROP TABLE match_queue")
                conn.execute("ALTER TABLE requests DROP COLUMN expires_at")
                conn.execute(
                    "ALTER TABLE requests DROP COLUMN quantity_requested")
                conn.execute("UPDATE schema_version SET version = 12"
                             " WHERE id = 1")
                conn.commit()
            finally:
                conn.close()
            # Migrating must not fail on the new indexes (they
            # reference backfilled columns) and must keep user data.
            init_db(db)
            conn = get_connection(db)
            try:
                version = conn.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()["version"]
                self.assertEqual(version, SCHEMA_VERSION)
                cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(requests)")}
                self.assertIn("expires_at", cols)
                self.assertIn("quantity_requested", cols)
                tables = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({"matches", "notifications",
                                 "match_queue", "twin_chats"} <= tables)
                kept = conn.execute(
                    "SELECT email FROM users"
                    " WHERE email = 'old@example.com'").fetchone()
                self.assertEqual(kept["email"], "old@example.com")
            finally:
                conn.close()


class TestInsertReturningId(unittest.TestCase):
    def test_sqlite_returns_new_id(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "ids.db")
            init_db(db)
            conn = get_connection(db)
            try:
                first = insert_returning_id(
                    conn, "INSERT INTO users (name, role, email)"
                    " VALUES (?, ?, ?)", ("A", "r", "a@x.test"))
                second = insert_returning_id(
                    conn, "INSERT INTO users (name, role, email)"
                    " VALUES (?, ?, ?)", ("B", "r", "b@x.test"))
                self.assertEqual((first, second), (1, 2))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
