"""Deploy-prep tests: PG adapter translation, schema parity, dual driver."""

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
              "usage_log", "memories"}

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
