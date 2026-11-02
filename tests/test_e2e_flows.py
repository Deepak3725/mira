"""End-to-end HTTP flows via a socket-free ASGI client.

Same coverage as before (register/login, twin create/edit, request
lifecycle, provider contact, conversation stepping, memory
import/approve, logout, user isolation, CSRF on every exercised
POST): full HTTP stack through the real ASGI app, isolated temp
SQLite database, no network, no live AI, no shared state.

``httpx.ASGITransport`` drives the app fully in-process without the
socketpair the starlette TestClient portal needs, which restricted
sandboxes forbid (WinError 10013). Lifespan setup is just
``init_db()`` (see ``app.main.lifespan``), run explicitly here.
"""

import asyncio
import os
import re
import selectors
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

if sys.platform == "win32":
    # Selector loop needs no socketpair (restricted sandboxes forbid
    # it); every request runs fully in-process on a fresh loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # The self-pipe is built eagerly in __init__ and used only as a
    # cross-thread wakeup fast path. Disable it at class level (the
    # sandbox forbids all socket creation) and let the periodic tick
    # in _run_sandboxed pick up threadpool completions by polling.
    asyncio.SelectorEventLoop._make_self_pipe = lambda self: None
    asyncio.SelectorEventLoop._close_self_pipe = lambda self: None
    # Both the base and selector-level wakeups: the Windows selector
    # loop overrides _write_to_self, so patching the base alone still
    # crashes worker-thread completion callbacks (hang).
    asyncio.BaseEventLoop._write_to_self = lambda self: None
    asyncio.SelectorEventLoop._write_to_self = lambda self: None
    # Windows select() rejects empty fd sets (WSAEINVAL); with no
    # sockets at all, sleeping through the timeout is exactly what
    # the empty case means on every other platform.
    _orig_select = selectors.SelectSelector._select

    def _select_or_sleep(self, rlist, wlist, xlist, timeout=None):
        if not rlist and not wlist and not xlist:
            if timeout is None:
                timeout = 0.005
            if timeout > 0:
                time.sleep(timeout)
            return ([], [], [])
        return _orig_select(self, rlist, wlist, xlist, timeout)

    selectors.SelectSelector._select = _select_or_sleep

from app import auth
from app import config as config_mod
from app.db import init_db
from app.main import app as _app


def _run_sandboxed(coro):
    """Run one coroutine without any socket creation.

    Windows event loops lazily build a socketpair self-pipe the first
    time a worker thread wakes the loop (``call_soon_threadsafe``),
    which restricted sandboxes forbid. The pipe is only a wakeup fast
    path: with wakeups disabled and a 5 ms periodic tick, the loop
    picks up threadpool completions by polling instead. Test
    infrastructure only; the application is untouched.
    """
    loop = asyncio.new_event_loop()

    def _tick():
        if not loop.is_closed():
            loop.call_later(0.005, _tick)

    loop.call_later(0.005, _tick)
    prev = asyncio.get_event_loop_policy().get_event_loop()
    asyncio.get_event_loop_policy().set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.get_event_loop_policy().set_event_loop(prev)
        loop.close()


class _SyncASGITransport(httpx.BaseTransport):
    """Drive the ASGI app in-process: no sockets, no portal threads.

    Each request runs the application on a fresh event loop and the
    httpx client layer above keeps working unchanged (cookies,
    redirects, multipart encoding, file uploads).
    """

    def __init__(self, app):
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return _run_sandboxed(self._handle(request))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme or "http",
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": request.url.query,
            "headers": [(k.lower(), v)
                        for k, v in request.headers.raw],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
        incoming = [{"type": "http.request", "body": body,
                     "more_body": False}]
        status = 500
        headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []

        async def receive():
            if incoming:
                return incoming.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers.extend(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        return httpx.Response(status, headers=headers,
                              content=b"".join(chunks),
                              request=request)


def _client() -> httpx.Client:
    return httpx.Client(transport=_SyncASGITransport(_app),
                        base_url="http://testserver",
                        follow_redirects=True)


def _hidden(html: str, name: str) -> str:
    match = re.search(r'name="%s" value="([^"]*)"' % re.escape(name), html)
    assert match, f"hidden field {name} missing"
    return match.group(1)


class E2ECase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "e2e.db")
        self._saved_env = {
            key: os.environ.get(key) for key in
            ("DATABASE_PATH", "DATABASE_URL", "MIRA_TEST_DATABASE_URL")}
        os.environ["DATABASE_PATH"] = self.db
        # Never let developer shell state redirect this suite at real PG.
        os.environ["DATABASE_URL"] = ""
        # MIRA_TEST_DATABASE_URL is deliberately left alone: unset/'sqlite'
        # -> the temp SQLite file above; set -> the shared mira_test PG
        # schema (tests/__init__.py sets the MIRA_TEST_MODE marker that
        # allows the redirect; the real server always ignores it).
        self._test_settings = config_mod.get_settings()
        self._patched = []
        for name, module in list(sys.modules.items()):
            if name == "app.config" or not name.startswith("app."):
                continue
            if getattr(module, "settings", None) is config_mod.settings:
                module.settings = self._test_settings
                self._patched.append(module)
        auth.reset_rate_limits()
        from app import db as db_mod

        assert db_mod.settings is self._test_settings, "settings patch missed app.db"
        init_db()
        self.client = _client()

    def tearDown(self):
        self.client.close()
        for module in self._patched:
            module.settings = config_mod.settings
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------
    def _register(self, client, name, email):
        token = _hidden(client.get("/register").text, "csrf_token")
        resp = client.post("/register", data={
            "name": name, "email": email, "password": "e2e-pass-99",
            "role": "professional", "csrf_token": token},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/me"))
        return client

    def _make_twin(self, client, display="E2E Twin"):
        token = _hidden(client.get("/twin/new").text, "csrf_token")
        resp = client.post("/twin/new", data={
            "display_name": display, "persona": "helpful",
            "tone": "friendly", "csrf_token": token},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _make_request(self, client, need, idem):
        page = client.get("/requests/new").text
        resp = client.post("/requests/new", data={
            "need_text": need, "category": "transport",
            "idem_key": idem, "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers["location"]
        self.assertIn("/review", loc)
        return re.search(r"/requests/(\d+)/review", loc).group(1)


class TestPublicFlows(E2ECase):
    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)

    def test_error_page_styled_and_safe(self):
        resp = self.client.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        body = resp.text
        self.assertIn("404 — Not found", body)
        self.assertIn("/static/css/style.css", body)
        self.assertNotIn("Traceback", body)

    def test_register_login_logout(self):
        self._register(self.client, "E2E Ada", "ada.e2e@example.org")
        self.assertEqual(self.client.get("/me").status_code, 200)
        token = _hidden(self.client.get("/me").text, "csrf_token")
        resp = self.client.post("/logout", data={"csrf_token": token},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = self.client.get("/me", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        # Log back in.
        token = _hidden(self.client.get("/login").text, "csrf_token")
        resp = self.client.post("/login", data={
            "email": "ada.e2e@example.org", "password": "e2e-pass-99",
            "csrf_token": token}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_csrf_rejected_on_auth_posts(self):
        resp = self.client.post("/register", data={
            "name": "X", "email": "x@example.org", "password": "e2e-pass-99"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/login", data={
            "email": "x@example.org", "password": "e2e-pass-99",
            "csrf_token": "forged"})
        self.assertEqual(resp.status_code, 403)

    def test_twin_create_and_edit(self):
        self._register(self.client, "E2E Bo", "bo.e2e@example.org")
        self._make_twin(self.client)
        page = self.client.get("/twin/edit").text
        resp = self.client.post("/twin/edit", data={
            "display_name": "E2E Twin v2", "persona": "helpful",
            "tone": "friendly", "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = self.client.post("/twin/edit", data={"display_name": "X"})
        self.assertEqual(resp.status_code, 403)

    def test_request_lifecycle_mock(self):
        self._register(self.client, "E2E Cy", "cy.e2e@example.org")
        self._make_twin(self.client)
        rid = self._make_request(
            self.client,
            "I need a taxi from GCIT to Thimphu tomorrow at 9 AM.", "e2e-1")
        review = self.client.get(f"/requests/{rid}/review").text
        self.assertIn("interpretation", review)
        resp = self.client.post(f"/requests/{rid}/confirm", data={
            "category": "transport", "budget": "500", "origin": "GCIT",
            "destination": "Thimphu", "when": "tomorrow at 9 AM",
            "skills": "", "csrf_token": _hidden(review, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = self.client.get(f"/requests/{rid}")
        self.assertEqual(detail.status_code, 200)
        resp = self.client.post(f"/requests/{rid}/close", data={})
        self.assertEqual(resp.status_code, 403)

    def test_user_isolation(self):
        bob = _client()
        self.addCleanup(bob.close)
        self._register(self.client, "E2E Ann", "ann.e2e@example.org")
        self._make_twin(self.client)
        rid = self._make_request(self.client, "I need a taxi from X to Y.", "e2e-2")
        self._register(bob, "E2E Bob", "bob.e2e@example.org")
        self.assertEqual(bob.get(f"/requests/{rid}/review").status_code, 404)
        self.assertEqual(bob.get(f"/requests/{rid}").status_code, 404)
        page = bob.get("/requests/new").text
        resp = bob.post(f"/requests/{rid}/confirm", data={
            "category": "other", "csrf_token": _hidden(page, "csrf_token")})
        self.assertEqual(resp.status_code, 404)

    def test_contact_and_conversation_step(self):
        from app import auth as auth_mod
        from app.db import (
            create_opportunity,
            create_twin,
            create_user,
            get_connection,
        )

        self._register(self.client, "E2E Dan", "dan.e2e@example.org")
        self._make_twin(self.client, "Dan Twin")
        rid = self._make_request(self.client, "I need a taxi from X to Y.", "e2e-3")
        review = self.client.get(f"/requests/{rid}/review").text
        self.client.post(f"/requests/{rid}/confirm", data={
            "category": "transport", "csrf_token": _hidden(review, "csrf_token")})
        conn = get_connection(self.db)
        try:
            prov_id = create_user(
                conn, name="E2E Prov", role="independent",
                email="prov.e2e@example.org",
                password_hash=auth_mod.hash_password("e2e-pass-99"))
            create_twin(conn, user_id=prov_id, display_name="Prov Twin",
                        persona="p", tone="t", profile={}, authorized_fields=[],
                        avatar_seed="prov")
            opp_id = create_opportunity(
                conn, owner_user_id=prov_id, kind="service",
                title="Taxi service", category="transport",
                attributes={"route": "X-Y"})
            conn.commit()
        finally:
            conn.close()
        detail = self.client.get(f"/requests/{rid}").text
        resp = self.client.post(f"/requests/{rid}/contact", data={
            "offering_id": str(opp_id),
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conv_url = resp.headers["location"]
        self.assertIn("/conversations/", conv_url)
        conv_id = re.search(r"/conversations/(\d+)", conv_url).group(1)
        page = self.client.get(conv_url)
        self.assertEqual(page.status_code, 200)
        resp = self.client.post(f"/conversations/{conv_id}/step", data={})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            f"/conversations/{conv_id}/step",
            data={"csrf_token": _hidden(page.text, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_memory_import_and_approve(self):
        from app.db import get_connection, get_twin_by_user, get_user_by_email

        self._register(self.client, "E2E Em", "em.e2e@example.org")
        self._make_twin(self.client)
        page = self.client.get("/memories").text
        payload = b'{"memories": [{"content": "I know Python well.", "source": "e2e"}]}'
        resp = self.client.post(
            "/memories/import",
            data={"csrf_token": _hidden(page, "csrf_token")},
            files={"export_file": ("exp.json", payload, "application/json")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "em.e2e@example.org")
            twin = get_twin_by_user(conn, user["id"])
            mem = conn.execute(
                "SELECT * FROM memories WHERE twin_id = ?",
                (twin["id"],)).fetchone()
        finally:
            conn.close()
        page = self.client.get("/memories").text
        resp = self.client.post(f"/memories/{mem['id']}/approve", data={
            "content": "", "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = self.client.post(f"/memories/{mem['id']}/delete", data={})
        self.assertEqual(resp.status_code, 403)

    def test_usage_page_mock_only(self):
        self._register(self.client, "E2E Fay", "fay.e2e@example.org")
        page = self.client.get("/usage").text
        self.assertIn("mock", page.lower())


if __name__ == "__main__":
    unittest.main()
