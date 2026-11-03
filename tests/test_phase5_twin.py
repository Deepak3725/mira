"""Phase 5 tests: session CSRF rotation + revocation, twin mind,
opportunity posting, security headers, v12 migration."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app import auth, memories, twin_mind
from app.db import (
    SCHEMA_VERSION,
    get_connection,
    init_db,
    insert_returning_id,
)


def _request(cookie_value=None, path="/"):
    headers = []
    if cookie_value:
        headers.append((b"cookie", f"{auth.SESSION_COOKIE}={cookie_value}"
                                   .encode("latin-1")))
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": headers})


def _hidden(html, name):
    match = re.search(r'name="%s" value="([^"]*)"' % re.escape(name), html)
    assert match, f"hidden field {name} missing"
    return match.group(1)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "s5.db")
        init_db(self.db)

    def tearDown(self):
        self._td.cleanup()

    def _conn(self):
        return get_connection(self.db)

    def _user(self, email="s5@example.org"):
        conn = self._conn()
        try:
            uid = insert_returning_id(
                conn, "INSERT INTO users (name, role, email)"
                " VALUES (?, ?, ?)", ("Ess", "member", email))
            conn.commit()
            return uid
        finally:
            conn.close()

    def _target_modules(self):
        import app.routes.accounts as accounts
        import app.routes.memories as memories_route
        import app.routes.pages as pages

        return accounts, memories_route, pages


def _drive(coro):
    """Run a coroutine that performs no real I/O (no event loop)."""
    try:
        yielded = coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError(f"unexpected suspension: {yielded!r}")


class TestSessionCsrf(unittest.TestCase):
    def test_bound_token_roundtrip(self):
        token = auth.session_csrf_token(7, "session-abc")
        self.assertTrue(auth.valid_session_csrf(token, 7, "session-abc"))
        self.assertFalse(auth.valid_session_csrf(token, 7, "other"))
        self.assertFalse(auth.valid_session_csrf(token, 8, "session-abc"))
        self.assertFalse(auth.valid_session_csrf("", 7, "session-abc"))

    def test_rotation_on_new_session(self):
        first = auth.session_csrf_token(7, "session-one")
        second = auth.session_csrf_token(7, "session-two")
        self.assertNotEqual(first, second)

    def test_legacy_scheme_untouched(self):
        token = auth.csrf_token(42)
        self.assertTrue(auth.valid_csrf(token, 42))
        self.assertFalse(auth.valid_csrf(token, 43))


class TestRevocation(SessionCase):
    def test_logout_invalidates_cookie(self):
        uid = self._user()
        session = auth.create_session(uid)
        factory = lambda *a, **k: self._conn()  # noqa: E731
        # current_user_id imports get_connection from app.db lazily.
        with mock.patch("app.db.get_connection", side_effect=factory):
            self.assertEqual(
                auth.current_user_id(_request(session)), uid)
            conn = self._conn()
            try:
                self.assertFalse(auth.session_revoked(conn, session))
                auth.revoke_session(conn, session)
                auth.revoke_session(conn, session)  # idempotent
                self.assertTrue(auth.session_revoked(conn, session))
            finally:
                conn.close()
            self.assertIsNone(
                auth.current_user_id(_request(session)))
            # A different TTL mints a distinct token: sessions minted
            # in the same second with the same TTL are identical.
            fresh = auth.create_session(uid, ttl=3600)
            self.assertNotEqual(fresh, session)
            self.assertEqual(
                auth.current_user_id(_request(fresh)), uid)


class TestRouteRoundtrip(SessionCase):
    def _patched(self):
        accounts, memories_route, _pages = self._target_modules()
        factory = lambda *a, **k: self._conn()  # noqa: E731
        return (mock.patch.object(accounts, "get_connection",
                                  side_effect=factory),
                mock.patch.object(memories_route, "get_connection",
                                  side_effect=factory),
                mock.patch("app.db.get_connection",
                           side_effect=factory))

    def _twin_with_memory(self, uid):
        from app.db import create_twin

        conn = self._conn()
        try:
            twin_id = create_twin(
                conn, user_id=uid, display_name="Ess", persona="p",
                tone="t", profile={}, authorized_fields=[],
                avatar_seed="s")
            mem_id = memories.create_memory(
                conn, twin_id=twin_id, content="I prefer tea.",
                source="hand", status="pending")
            conn.commit()
        finally:
            conn.close()
        return twin_id, mem_id

    def test_memory_approve_roundtrip(self):
        from app.routes import memories as memories_route

        uid = self._user()
        _twin_id, mem_id = self._twin_with_memory(uid)
        session = auth.create_session(uid)
        patches = self._patched()
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        page = memories_route.memory_list(_request(session)).body.decode()
        token = _hidden(page, "csrf_token")
        resp = memories_route.memory_approve(
            _request(session), mem_id, content="I prefer tea.",
            csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            row = memories.get_memory(conn, mem_id)
        finally:
            conn.close()
        self.assertEqual(row["status"], "approved")

    def test_cross_session_token_rejected(self):
        from fastapi import HTTPException

        from app.routes import memories as memories_route

        uid = self._user()
        _twin_id, mem_id = self._twin_with_memory(uid)
        patches = self._patched()
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        # Distinct TTLs mint distinct tokens (same-second sessions
        # with the same TTL are identical by stateless design).
        victim = auth.create_session(uid, ttl=3600)
        attacker = auth.create_session(uid, ttl=7200)
        self.assertNotEqual(victim, attacker)
        stolen = auth.csrf_for_request(_request(victim), uid)
        with self.assertRaises(HTTPException) as ctx:
            memories_route.memory_approve(
                _request(attacker), mem_id,
                content="I prefer tea.", csrf_token=stolen)
        self.assertEqual(ctx.exception.status_code, 403)
        legacy = auth.csrf_token(uid)
        with self.assertRaises(HTTPException):
            memories_route.memory_approve(
                _request(attacker), mem_id,
                content="I prefer tea.", csrf_token=legacy)

    def test_logout_revokes_session(self):
        from app.routes import accounts as accounts_route

        uid = self._user()
        session = auth.create_session(uid)
        patches = self._patched()
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        token = auth.csrf_for_request(_request(session), uid)
        resp = accounts_route.logout(_request(session), csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        self.assertIn(auth.SESSION_COOKIE, resp.headers["set-cookie"])
        self.assertIsNone(auth.current_user_id(_request(session)))

    def test_twin_edit_shows_public_preview(self):
        from app.routes import accounts as accounts_route

        uid = self._user()
        session = auth.create_session(uid)
        accounts, _mem, _pages = self._target_modules()
        factory = lambda *a, **k: self._conn()  # noqa: E731
        patches = [mock.patch.object(accounts, "get_connection",
                                     side_effect=factory),
                   mock.patch("app.db.get_connection",
                              side_effect=factory)]
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        from app.db import create_twin

        conn = self._conn()
        try:
            create_twin(
                conn, user_id=uid, display_name="Ess", persona="p",
                tone="t",
                profile={"headline": "Backend dev",
                         "skills": ["Python"],
                         "phone": "+975-00-SECRET"},
                authorized_fields=["headline", "skills"],
                avatar_seed="s")
            conn.commit()
        finally:
            conn.close()
        page = accounts_route.twin_edit_form(
            _request(session)).body.decode()
        self.assertIn("what other twins see", page)
        preview = page.split("what other twins see", 1)[1]
        preview = preview.split("</section>", 1)[0]
        self.assertIn("Backend dev", preview)
        self.assertIn("Python", preview)
        self.assertNotIn("+975-00-SECRET", preview)

    def test_twin_update_roundtrip(self):
        from app.routes import accounts as accounts_route

        uid = self._user()
        session = auth.create_session(uid)
        accounts, _mem, _pages = self._target_modules()
        factory = lambda *a, **k: self._conn()  # noqa: E731
        patches = [mock.patch.object(accounts, "get_connection",
                                     side_effect=factory),
                   mock.patch("app.db.get_connection",
                              side_effect=factory)]
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        from app.db import create_twin, get_twin

        conn = self._conn()
        try:
            twin_id = create_twin(
                conn, user_id=uid, display_name="Ess", persona="p",
                tone="t", profile={}, authorized_fields=[],
                avatar_seed="s")
            conn.commit()
        finally:
            conn.close()
        page = accounts_route.twin_edit_form(
            _request(session)).body.decode()
        token = _hidden(page, "csrf_token")
        resp = accounts_route.twin_update(
            _request(session), display_name="Ess Twin",
            persona="helpful", tone="warm", headline="", skills="",
            experience_years="", location="", availability="",
            portfolio_url="", bio="", company="", role_hiring="",
            requirements="", phone="", personal_email="",
            open_to_remote=None, authorized=[], public_listing=None,
            cond_max_price="", cond_auto_answer=None,
            cond_require_approval=None, cond_distance="",
            cond_availability="", csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        conn = self._conn()
        try:
            twin = get_twin(conn, twin_id)
        finally:
            conn.close()
        self.assertEqual(twin["display_name"], "Ess Twin")


class TestOpportunityCreate(SessionCase):
    def _start(self):
        import app.routes.accounts as accounts
        import app.routes.pages as pages

        factory = lambda *a, **k: self._conn()  # noqa: E731
        patches = [
            mock.patch.object(accounts, "get_connection",
                              side_effect=factory),
            mock.patch.object(pages, "get_connection",
                              side_effect=factory),
            mock.patch("app.db.get_connection",
                       side_effect=factory),
        ]
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _form_token(self, session):
        from app.routes import pages as pages_route

        page = pages_route.opportunity_new_form(
            _request(session)).body.decode()
        kinds = pages_route.OPP_KINDS
        for kind in kinds:
            self.assertIn(kind, page)
        return _hidden(page, "csrf_token")

    def _count(self):
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities").fetchone()["n"]
        finally:
            conn.close()

    def test_form_requires_login(self):
        from app.routes import pages as pages_route

        self._start()
        resp = pages_route.opportunity_new_form(_request())
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_create_roundtrip_and_detail(self):
        from app.db import get_opportunity
        from app.routes import pages as pages_route

        uid = self._user()
        session = auth.create_session(uid)
        self._start()
        token = self._form_token(session)
        resp = pages_route.opportunity_create(
            _request(session), kind="service", title="Fix my bike",
            description="Brakes squeak.", skills="repair, bikes",
            years="2", location="Oslo", remote_ok="", category="",
            csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertRegex(location, r"^/opportunities/\d+$")
        opp_id = int(location.rsplit("/", 1)[1])
        conn = self._conn()
        try:
            opp = get_opportunity(conn, opp_id)
        finally:
            conn.close()
        self.assertEqual(opp["title"], "Fix my bike")
        self.assertEqual(opp["kind"], "service")
        self.assertEqual(opp["owner_user_id"], uid)
        self.assertIn("repair", opp["required_skills_json"])
        detail = pages_route.opportunity_detail(
            _request(session), opp_id).body.decode()
        self.assertIn("Fix my bike", detail)

    def test_bad_kind_rejected(self):
        from app.routes import pages as pages_route

        uid = self._user()
        session = auth.create_session(uid)
        self._start()
        resp = pages_route.opportunity_create(
            _request(session), kind="spaceship", title="Fly me",
            description="", skills="", years="", location="",
            remote_ok="", category="",
            csrf_token=self._form_token(session))
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=bad-kind", resp.headers["location"])
        self.assertEqual(self._count(), 0)

    def test_missing_title_rejected(self):
        from app.routes import pages as pages_route

        uid = self._user()
        session = auth.create_session(uid)
        self._start()
        resp = pages_route.opportunity_create(
            _request(session), kind="job", title="   ",
            description="", skills="", years="", location="",
            remote_ok="", category="",
            csrf_token=self._form_token(session))
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=need-title", resp.headers["location"])
        self.assertEqual(self._count(), 0)

    def test_wrong_csrf_rejected(self):
        from fastapi import HTTPException
        from app.routes import pages as pages_route

        uid = self._user()
        session = auth.create_session(uid)
        self._start()
        with self.assertRaises(HTTPException) as ctx:
            pages_route.opportunity_create(
                _request(session), kind="job", title="Nope",
                description="", skills="", years="", location="",
                remote_ok="", category="", csrf_token="bogus")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._count(), 0)

    def test_discover_links_post_form(self):
        discover = (Path(__file__).resolve().parent.parent
                    / "app" / "templates" / "discover.html")
        self.assertIn("/opportunities/new",
                      discover.read_text(encoding="utf-8"))


class TestTwinMind(SessionCase):
    def _twin(self, uid, public=False):
        from app.db import create_twin

        conn = self._conn()
        try:
            twin_id = create_twin(
                conn, user_id=uid, display_name="Tee", persona="p",
                tone="t", profile={}, authorized_fields=[],
                avatar_seed="s", is_public=public)
            conn.commit()
        finally:
            conn.close()
        return twin_id

    def _add(self, twin_id, content, status="approved",
             category="preference", source="import"):
        conn = self._conn()
        try:
            mem_id = memories.create_memory(
                conn, twin_id=twin_id, content=content,
                source=source, status=status, category=category)
            conn.commit()
        finally:
            conn.close()
        return mem_id

    def test_answers_from_approved_only(self):
        uid = self._user()
        twin_id = self._twin(uid)
        self._add(twin_id, "I prefer tea over coffee.")
        self._add(twin_id, "My secret plan stays hidden.",
                   status="pending")
        self._add(twin_id, "Answer briefly.", category="style",
                   source="hand")
        conn = self._conn()
        try:
            out = twin_mind.answer_question(
                conn, twin_id, "Do I like tea?")
        finally:
            conn.close()
        self.assertIn("tea over coffee", out["answer"])
        self.assertNotIn("hidden", out["answer"])
        self.assertIn("briefly", out["answer"])
        self.assertFalse(out["used_ai"])
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(len(out["sources"]), 1)
        self.assertEqual(out["sources"][0]["source"], "import")
        self.assertIn("category", out["sources"][0])

    def test_two_matches_high_confidence(self):
        uid = self._user()
        twin_id = self._twin(uid)
        self._add(twin_id, "I drink tea every morning.")
        self._add(twin_id, "I prefer tea over coffee.")
        conn = self._conn()
        try:
            out = twin_mind.answer_question(conn, twin_id, "tea")
        finally:
            conn.close()
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(len(out["sources"]), 2)

    def test_fallback_when_nothing_matches(self):
        uid = self._user()
        twin_id = self._twin(uid)
        self._add(twin_id, "I prefer tea over coffee.")
        conn = self._conn()
        try:
            out = twin_mind.answer_question(
                conn, twin_id, "quantum field theory")
        finally:
            conn.close()
        self.assertIn("don't know", out["answer"])
        self.assertEqual(out["sources"], [])
        self.assertEqual(out["confidence"], "none")
        self.assertFalse(out["used_ai"])

    def test_answer_question_writes_nothing(self):
        uid = self._user()
        twin_id = self._twin(uid)
        self._add(twin_id, "I prefer tea over coffee.")
        conn = self._conn()
        try:
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: conn.execute(
                f'SELECT COUNT(*) AS n FROM "{t}"').fetchone()["n"]
                for t in tables}
            twin_mind.answer_question(conn, twin_id, "tea")
            after = {t: conn.execute(
                f'SELECT COUNT(*) AS n FROM "{t}"').fetchone()["n"]
                for t in tables}
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_request_owner_input_audits(self):
        from app import conversations as engine

        owner = self._user("owner@example.org")
        other = self._user("other@example.org")
        twin_a = self._twin(owner)
        twin_b = self._twin(other, public=True)
        conn = self._conn()
        try:
            conv_id = engine.create_conversation(
                conn, twin_a_id=twin_a, twin_b_id=twin_b,
                created_by_user_id=owner)
            audit_id = twin_mind.request_owner_input(
                conn, conv_id, owner, "Should I accept?")
            conn.commit()
            row = conn.execute(
                "SELECT * FROM audit_events WHERE id = ?",
                (audit_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["action"], "owner.input_requested")
        self.assertEqual(row["conv_id"], conv_id)
        self.assertIn("accept", row["detail"])

    def test_request_owner_input_validates(self):
        from app import conversations as engine

        owner = self._user("owner2@example.org")
        other = self._user("other2@example.org")
        twin_a = self._twin(owner)
        twin_b = self._twin(other, public=True)
        conn = self._conn()
        try:
            conv_id = engine.create_conversation(
                conn, twin_a_id=twin_a, twin_b_id=twin_b,
                created_by_user_id=owner)
            with self.assertRaises(ValueError):
                twin_mind.request_owner_input(
                    conn, conv_id + 999, owner, "Hello?")
            with self.assertRaises(ValueError):
                twin_mind.request_owner_input(
                    conn, conv_id, owner, "   ")
        finally:
            conn.close()


class TestSecurityHeaders(unittest.TestCase):
    def test_headers_applied(self):
        from starlette.responses import PlainTextResponse

        from app.main import SECURITY_HEADERS, security_headers

        async def call_next(request):
            return PlainTextResponse("ok")

        resp = _drive(security_headers(_request(), call_next))
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers[name], value)
        self.assertGreaterEqual(len(SECURITY_HEADERS), 3)

    def test_registered_on_app(self):
        main_py = (Path(__file__).resolve().parent.parent
                   / "app" / "main.py")
        self.assertIn('app.middleware("http")(security_headers)',
                      main_py.read_text(encoding="utf-8"))


class TestSchemaV12(SessionCase):
    def test_version_and_revoked_table(self):
        # v15 adds opportunities.expires_at + matches.accept_message.
        self.assertEqual(SCHEMA_VERSION, 15)
        conn = self._conn()
        try:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("revoked_sessions", tables)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            self.assertEqual(row["version"], SCHEMA_VERSION)
        finally:
            conn.close()
