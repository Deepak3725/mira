"""Phase 1 maintained tests: DB, twins, privacy filter, avatars (stdlib only)."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app import avatars, privacy
from app.db import (
    SCHEMA_VERSION,
    create_twin,
    create_user,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    list_twins,
)
from app.seed import SEED_USERS, seed_demo_data


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()


class TestInit(DbTestCase):
    def test_tables_exist_and_version_set(self):
        from app.db import driver_name, get_connection

        conn = get_connection(self.db)
        try:
            if driver_name(conn) == "postgres":
                rows = conn.execute(
                    "SELECT table_name AS name FROM information_schema.tables"
                    " WHERE table_schema = current_schema()"
                )
            else:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            tables = {r["name"] for r in rows}
            self.assertTrue({"schema_version", "users", "twins"} <= tables)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            self.assertEqual(row["version"], SCHEMA_VERSION)
        finally:
            conn.close()


class TestUsersAndTwins(DbTestCase):
    def test_create_and_relationship(self):
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            uid = create_user(conn, name="A", role="job_seeker", email="a@x.test")
            tid = create_twin(
                conn,
                user_id=uid,
                display_name="Ay",
                persona="p",
                tone="t",
                profile={"skills": ["Python"], "phone": "secret"},
                authorized_fields=["skills"],
                avatar_seed="seed-a",
            )
            conn.commit()
            twin = get_twin_by_user(conn, uid)
            self.assertEqual(twin["id"], tid)
            self.assertEqual(twin["user_id"], uid)
            self.assertEqual(twin["owner_name"], "A")
        finally:
            conn.close()

    def test_one_twin_per_user(self):
        from app.db import INTEGRITY_ERRORS, get_connection

        conn = get_connection(self.db)
        try:
            uid = create_user(conn, name="B", role="employer", email="b@x.test")
            kwargs = dict(
                display_name="By", persona="p", tone="t",
                profile={}, authorized_fields=[], avatar_seed="s",
            )
            create_twin(conn, user_id=uid, **kwargs)
            with self.assertRaises(INTEGRITY_ERRORS):
                create_twin(conn, user_id=uid, **kwargs)
        finally:
            conn.close()

    def test_twin_requires_existing_user(self):
        from app.db import INTEGRITY_ERRORS, get_connection

        conn = get_connection(self.db)
        try:
            with self.assertRaises(INTEGRITY_ERRORS):
                create_twin(
                    conn, user_id=9999, display_name="X", persona="p",
                    tone="t", profile={}, authorized_fields=[], avatar_seed="s",
                )
        finally:
            conn.close()

    def test_list_twins_joins_owner(self):
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            uid = create_user(conn, name="C", role="professional", email="c@x.test")
            create_twin(
                conn, user_id=uid, display_name="Cy", persona="p",
                tone="t", profile={}, authorized_fields=[], avatar_seed="s",
            )
            conn.commit()
            rows = list_twins(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["owner_email"], "c@x.test")
        finally:
            conn.close()


class TestPrivacy(unittest.TestCase):
    PROFILE = {
        "headline": "Dev",
        "skills": ["Python"],
        "phone": "+975-00-000",
        "personal_email": "priv@x.test",
        "salary_expectation": "lots",
        "home_address": "secret lane",
    }

    def test_only_authorized_pass(self):
        out = privacy.authorized_view(self.PROFILE, ["headline", "skills"])
        self.assertEqual(out, {"headline": "Dev", "skills": ["Python"]})

    def test_private_fields_blocked(self):
        out = privacy.authorized_view(self.PROFILE, ["headline"])
        for secret in ("phone", "personal_email", "salary_expectation", "home_address"):
            self.assertNotIn(secret, out)

    def test_unknown_and_nonstr_keys_ignored(self):
        out = privacy.authorized_view(self.PROFILE, ["headline", "nope", 42, None])
        self.assertEqual(out, {"headline": "Dev"})

    def test_empty_or_none_allowlist(self):
        self.assertEqual(privacy.authorized_view(self.PROFILE, []), {})
        self.assertEqual(privacy.authorized_view(self.PROFILE, None), {})

    def test_input_not_mutated(self):
        profile = dict(self.PROFILE)
        privacy.authorized_view(profile, ["headline"])
        self.assertEqual(profile, self.PROFILE)

    def test_twin_public_view_parses_row(self):
        from app.db import get_connection

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            init_db(db)
            conn = get_connection(db)
            try:
                uid = create_user(conn, name="D", role="job_seeker", email="d@x.test")
                create_twin(
                    conn, user_id=uid, display_name="Dy", persona="p", tone="t",
                    profile=self.PROFILE, authorized_fields=["skills"],
                    avatar_seed="s",
                )
                conn.commit()
                view = privacy.twin_public_view(get_twin_by_user(conn, uid))
                self.assertEqual(view, {"skills": ["Python"]})
            finally:
                conn.close()


class TestAvatars(unittest.TestCase):
    def test_valid_svg(self):
        svg = avatars.avatar_svg("deepak-companion-01")
        self.assertTrue(svg.startswith("<svg"))
        ET.fromstring(svg)  # raises if malformed

    def test_deterministic(self):
        self.assertEqual(avatars.avatar_svg("abc"), avatars.avatar_svg("abc"))

    def test_distinct_seeds_differ(self):
        self.assertNotEqual(avatars.avatar_svg("seed-1"), avatars.avatar_svg("seed-2"))

    def test_all_states_valid(self):
        for state in avatars.STATES:
            svg = avatars.avatar_svg("abc", state)
            ET.fromstring(svg)
            self.assertIn(f"state-{state}", svg)

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            avatars.avatar_svg("abc", "dancing")


class TestSeed(DbTestCase):
    def test_seeds_four_users_with_twins(self):
        result = seed_demo_data(self.db)
        self.assertEqual(result, {"created": 4, "skipped": 0})
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            rows = list_twins(conn)
            self.assertEqual(len(rows), 4)
            names = sorted(r["owner_name"] for r in rows)
            self.assertEqual(names, ["Deepak", "Karma", "Pema", "Tashi"])
            for row in rows:
                view = privacy.twin_public_view(row)
                for secret in ("phone", "personal_email", "salary_expectation",
                               "hiring_budget_cap", "day_rate_floor", "client_list",
                               "home_address", "internal_notes"):
                    self.assertNotIn(secret, view)
        finally:
            conn.close()

    def test_seed_is_idempotent(self):
        seed_demo_data(self.db)
        result = seed_demo_data(self.db)
        self.assertEqual(result, {"created": 0, "skipped": 4})
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            self.assertEqual(len(list_twins(conn)), 4)
        finally:
            conn.close()

    def test_sequential_seeds_preserve_prior_data(self):
        from app.db import get_connection, get_user_by_email
        from app.seed import seed_demo_opportunities

        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        conn = get_connection(self.db)
        try:
            self.assertIsNotNone(
                get_user_by_email(conn, "deepak.demo@example.com"))
            self.assertEqual(len(list_twins(conn)), 4)
        finally:
            conn.close()

    def test_seed_matches_declared_users(self):
        from app.db import get_connection

        self.assertEqual(len(SEED_USERS), 4)
        self.assertEqual(
            sorted(u["name"] for u in SEED_USERS),
            ["Deepak", "Karma", "Pema", "Tashi"],
        )
        conn = get_connection(self.db)
        try:
            self.assertIsNone(get_user_by_email(conn, "nobody@x.test"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
