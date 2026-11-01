"""Phase 6 tests: voice parsing, providers, turn handling, routes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app import auth
from app.channels import outbox
from app.db import create_twin, get_connection, init_db, insert_returning_id
from app.voice import (
    commands,
    handle_transcript,
    providers,
)
from app.voice import registry as voice_registry
from app.voice.session import reset_sessions
from app.voice.types import UnsupportedOperation, VoiceError


def _request(cookie_value=None):
    headers = []
    if cookie_value:
        headers.append((b"cookie", f"{auth.SESSION_COOKIE}={cookie_value}"
                                   .encode("latin-1")))
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": headers})


class TestParser(unittest.TestCase):
    def test_example_message_parses_fully(self):
        cmd = commands.parse_command(
            "Hii twin, send a message to Karma on my Messenger"
            " at 11 PM and tell him I missed him")
        self.assertEqual(cmd["kind"], "message")
        self.assertEqual(cmd["slots"]["contact"], "Karma")
        self.assertEqual(cmd["slots"]["channel"], "messenger")
        self.assertEqual(cmd["slots"]["when"], "11 PM")
        self.assertEqual(cmd["slots"]["body"], "I missed him")
        self.assertEqual(cmd["missing"], [])

    def test_message_missing_slots_listed(self):
        cmd = commands.parse_command("send a message")
        self.assertEqual(cmd["kind"], "message")
        self.assertEqual(cmd["missing"],
                         ["contact", "channel", "body"])

    def test_quoted_body_wins(self):
        cmd = commands.parse_command(
            'text Ana on telegram "running ten minutes late"')
        self.assertEqual(cmd["slots"]["contact"], "Ana")
        self.assertEqual(cmd["slots"]["channel"], "telegram")
        self.assertEqual(cmd["slots"]["body"],
                         "running ten minutes late")

    def test_saying_keeps_message_verbs(self):
        cmd = commands.parse_command(
            "send a message to Karma on mock saying see you soon")
        self.assertEqual(cmd["slots"]["body"], "see you soon")

    def test_contact_name_consumed_after_tell(self):
        cmd = commands.parse_command(
            "send to Karma on mock telling Karma guess what")
        self.assertEqual(cmd["slots"]["contact"], "Karma")
        self.assertEqual(cmd["slots"]["body"], "guess what")

    def test_tell_me_is_a_question(self):
        cmd = commands.parse_command("tell me about my week")
        self.assertEqual(cmd["kind"], "ask")

    def test_question_mark_asks(self):
        cmd = commands.parse_command("do I like tea?")
        self.assertEqual(cmd["kind"], "ask")

    def test_project_title_extracted(self):
        cmd = commands.parse_command("build me an e-commerce website")
        self.assertEqual(cmd["kind"], "project")
        self.assertEqual(cmd["slots"]["title"], "e-commerce website")

    def test_project_without_title_asks(self):
        cmd = commands.parse_command("start")
        self.assertEqual(cmd["kind"], "project")
        self.assertEqual(cmd["missing"], ["title"])

    def test_cancel_help_status(self):
        self.assertEqual(commands.parse_command("cancel")["kind"],
                         "cancel")
        self.assertEqual(commands.parse_command("help")["kind"],
                         "help")
        self.assertEqual(
            commands.parse_command("what is my status")["kind"],
            "status")

    def test_unknown_stays_unknown(self):
        cmd = commands.parse_command("Karma")
        self.assertEqual(cmd["kind"], "unknown")

    def test_affirm_deny(self):
        self.assertTrue(commands.is_affirm("yes"))
        self.assertTrue(commands.is_affirm("Send it"))
        self.assertFalse(commands.is_affirm("yesterday"))
        self.assertTrue(commands.is_deny("no"))
        self.assertTrue(commands.is_deny("cancel"))
        self.assertFalse(commands.is_deny("yes"))

    def test_channel_word(self):
        self.assertEqual(commands.channel_word("Messenger"),
                         "messenger")
        self.assertIsNone(commands.channel_word("pigeon"))


class TestProviders(unittest.TestCase):
    def test_browser_accepts_text_only(self):
        provider = providers.BrowserVoiceProvider()
        self.assertTrue(provider.capabilities.client_side)
        out = provider.accept_transcript("  hello ")
        self.assertEqual((out.text, out.provider, out.mock),
                         ("hello", "browser", False))
        with self.assertRaises(UnsupportedOperation):
            provider.transcribe(b"audio-bytes")
        with self.assertRaises(VoiceError):
            provider.accept_transcript("   ")

    def test_mock_script(self):
        provider = providers.MockVoiceProvider(script=("hi",))
        out = provider.transcribe(b"bytes")
        self.assertEqual((out.text, out.mock), ("hi", True))
        with self.assertRaises(VoiceError):
            provider.transcribe(b"more")

    def test_meta_stub_refuses(self):
        provider = providers.MetaTranscribeProvider()
        with self.assertRaises(UnsupportedOperation):
            provider.transcribe(b"bytes")
        with self.assertRaises(UnsupportedOperation):
            provider.accept_transcript("hi")

    def test_registry(self):
        self.assertIn("browser", voice_registry.available())
        self.assertIn("meta", voice_registry.available())
        self.assertEqual(voice_registry.get_provider("").name,
                         "browser")
        self.assertEqual(
            voice_registry.get_provider("pigeon").name, "browser")
        self.assertEqual(voice_registry.get_provider("mock").name,
                         "mock")


class VoiceCase(unittest.TestCase):
    def setUp(self):
        reset_sessions()
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "v6.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.owner = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email)"
            " VALUES (?, ?, ?)", ("Vox", "member", "vox@example.org"))
        self.other = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email)"
            " VALUES (?, ?, ?)", ("Oth", "member", "vox2@example.org"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()
        reset_sessions()

    def _twin(self, uid):
        twin_id = create_twin(
            self.conn, user_id=uid, display_name="Vox",
            persona="p", tone="t", profile={},
            authorized_fields=[], avatar_seed="s")
        self.conn.commit()
        return twin_id

    def _account(self, owner=None, channel="mock", label="Mock"):
        account_id = outbox.create_account(
            self.conn, self.owner if owner is None else owner,
            channel, label, credentials={})
        outbox.activate_account(
            self.conn, account_id,
            self.owner if owner is None else owner)
        self.conn.commit()
        return account_id

    def _contact(self, account_id, name="Karma", external="777"):
        contact_id = outbox.add_contact(
            self.conn, account_id, self.owner, external_id=external,
            display_name=name)
        self.conn.commit()
        return contact_id

    def _turn(self, text, user=None, twin=None, provider="text"):
        return handle_transcript(
            self.conn, user_id=self.owner if user is None else user,
            twin_id=twin, text=text, provider=provider)

    def _audit_kinds(self, user):
        return [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE actor_user_id = ?"
            " ORDER BY id", (user,))]


class TestTurns(VoiceCase):
    def test_ask_uses_approved_memories(self):
        from app import memories

        twin_id = self._twin(self.owner)
        memories.create_memory(
            self.conn, twin_id=twin_id,
            content="I prefer tea over coffee.", source="import",
            status="approved")
        self.conn.commit()
        reply = self._turn("do I like tea?", twin=twin_id)
        self.assertIn("tea over coffee", reply["text"])
        self.assertTrue(reply["done"])
        self.assertFalse(reply["expect_confirmation"])
        self.assertEqual(reply["provider"], "text")

    def test_ask_without_twin(self):
        reply = self._turn("do I like tea?", twin=None)
        self.assertIn("Set up your twin", reply["text"])

    def test_help_and_status(self):
        reply = self._turn("help")
        self.assertIn("confirm before sending", reply["text"])
        twin_id = self._twin(self.owner)
        reply = self._turn("what is my status", twin=twin_id)
        self.assertIn("0 project(s)", reply["text"])
        self.assertIn("free", reply["text"])

    def test_message_confirm_sends_mock(self):
        account_id = self._account()
        self._contact(account_id)
        twin_id = self._twin(self.owner)
        first = self._turn("send a message to Karma on mock saying hi",
                           twin=twin_id)
        self.assertTrue(first["expect_confirmation"])
        self.assertIn("Karma", first["text"])
        self.assertIn("Mock", first["text"])
        second = self._turn("yes", twin=twin_id)
        self.assertEqual(second["text"], "Sent.")
        row = self.conn.execute(
            "SELECT status FROM outbox_messages").fetchone()
        self.assertEqual(row["status"], "sent")

    def test_message_no_cancels_draft(self):
        account_id = self._account()
        self._contact(account_id)
        self._turn("send a message to Karma on mock saying hi")
        reply = self._turn("no")
        self.assertIn("Cancelled", reply["text"])
        row = self.conn.execute(
            "SELECT status FROM outbox_messages").fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_scheduled_message_queues_honestly(self):
        account_id = self._account()
        self._contact(account_id)
        first = self._turn("send a message to Karma on mock at 11 PM"
                           " saying goodnight")
        self.assertIn("assumed UTC", first["text"])
        second = self._turn("yes")
        self.assertIn("scheduler runs", second["text"])
        row = self.conn.execute(
            "SELECT status, scheduled_for FROM outbox_messages"
        ).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["scheduled_for"])

    def test_empty_transcript_raises(self):
        with self.assertRaises(VoiceError):
            self._turn("   ")

    def test_meta_provider_refuses(self):
        with self.assertRaises(UnsupportedOperation):
            self._turn("hello", provider="meta")

    def test_ambiguous_contact_never_sends(self):
        account_id = self._account()
        self._contact(account_id, name="Karma", external="1")
        self._contact(account_id, name="Karma Jr", external="2")
        reply = self._turn("send a message to karm on mock saying hi")
        self.assertIn("several", reply["text"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox_messages").fetchone()
        self.assertEqual(count["n"], 0)

    def test_unknown_contact_refuses(self):
        self._account()
        reply = self._turn(
            "send a message to Nobody on mock saying hi")
        self.assertIn("no contact", reply["text"].lower())

    def test_no_account_refuses(self):
        reply = self._turn(
            "send a message to Karma on mock saying hi")
        self.assertIn("no active", reply["text"].lower())

    def test_collect_flow_fills_slots(self):
        account_id = self._account()
        self._contact(account_id)
        first = self._turn("send a message")
        self.assertIn("Who", first["text"])
        self.assertFalse(first["done"])
        second = self._turn("Karma")
        self.assertIn("Which app", second["text"])
        third = self._turn("mock")
        self.assertIn("What should", third["text"])
        fourth = self._turn("hello there")
        self.assertTrue(fourth["expect_confirmation"])
        fifth = self._turn("yes")
        self.assertEqual(fifth["text"], "Sent.")

    def test_collect_rejects_bad_channel(self):
        self._account()
        self._turn("send a message")
        self._turn("Karma")
        reply = self._turn("pigeon")
        self.assertIn("Which app", reply["text"])

    def test_new_command_interrupts_collect(self):
        twin_id = self._twin(self.owner)
        self._turn("send a message")
        reply = self._turn("what is my status", twin=twin_id)
        self.assertIn("Dropping", reply["text"])
        self.assertIn("project(s)", reply["text"])

    def test_new_command_cancels_unconfirmed_draft(self):
        account_id = self._account()
        self._contact(account_id)
        self._turn("send a message to Karma on mock saying hi")
        reply = self._turn("help")
        self.assertIn("Dropping the unconfirmed", reply["text"])
        row = self.conn.execute(
            "SELECT status FROM outbox_messages").fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_project_confirm_creates(self):
        first = self._turn("build an e-commerce website")
        self.assertTrue(first["expect_confirmation"])
        second = self._turn("yes")
        self.assertIn("Created project #", second["text"])
        row = self.conn.execute(
            "SELECT title FROM projects").fetchone()
        self.assertEqual(row["title"], "e-commerce website")

    def test_project_collect_and_deny(self):
        first = self._turn("start")
        self.assertIn("call the project", first["text"])
        second = self._turn("Treehouse")
        self.assertTrue(second["expect_confirmation"])
        third = self._turn("no")
        self.assertIn("Cancelled", third["text"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM projects").fetchone()
        self.assertEqual(count["n"], 0)

    def test_users_isolated(self):
        account_id = self._account()
        self._contact(account_id)
        self._turn("send a message to Karma on mock saying hi")
        reply = self._turn("help", user=self.other)
        self.assertNotIn("Dropping", reply["text"])
        mine = self._turn("no")  # my pending is still there
        self.assertIn("Cancelled", mine["text"])

    def test_turns_audited(self):
        self._turn("help")
        self._turn("help", user=self.other)
        self.assertEqual(self._audit_kinds(self.owner),
                         ["voice.command"])
        self.assertEqual(self._audit_kinds(self.other),
                         ["voice.command"])


class TestVoiceRoutes(VoiceCase):
    def _start(self):
        import app.routes.accounts as accounts
        import app.routes.voice as voice_routes

        self.conn.close()  # patched calls open their own handles
        factory = lambda *a, **k: get_connection(self.db)  # noqa: E731
        patches = [
            mock.patch.object(accounts, "get_connection",
                              side_effect=factory),
            mock.patch.object(voice_routes, "get_connection",
                              side_effect=factory),
            mock.patch("app.db.get_connection",
                       side_effect=factory),
        ]
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.conn = get_connection(self.db)

    def _csrf(self, session):
        import app.routes.voice as voice_routes

        page = voice_routes.voice_page(_request(session)).body.decode()
        import re

        match = re.search(r"const CSRF_TOKEN = \"([^\"]+)\"", page)
        self.assertIsNotNone(match)
        return page, match.group(1)

    def test_page_requires_login(self):
        import app.routes.voice as voice_routes

        self._start()
        resp = voice_routes.voice_page(_request())
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_page_renders_providers(self):
        uid = self.owner
        session = auth.create_session(uid)
        self._start()
        page, _token = self._csrf(session)
        for name in ("browser", "text", "mock", "meta"):
            self.assertIn(f'value="{name}"', page)
        self.assertIn("Start listening", page)

    def test_command_roundtrip(self):
        import app.routes.voice as voice_routes

        session = auth.create_session(self.owner)
        self._start()
        _page, token = self._csrf(session)
        resp = voice_routes.voice_command(
            _request(session), transcript="help", provider="text",
            csrf_token=token)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body.decode())
        self.assertIn("confirm before sending", data["text"])
        self.assertEqual(data["provider"], "text")
        self.assertFalse(data["mock"])

    def test_command_requires_login(self):
        import app.routes.voice as voice_routes

        self._start()
        resp = voice_routes.voice_command(
            _request(), transcript="help", provider="text",
            csrf_token="x")
        self.assertEqual(resp.status_code, 401)

    def test_command_rejects_bad_csrf(self):
        from fastapi import HTTPException
        import app.routes.voice as voice_routes

        session = auth.create_session(self.owner)
        self._start()
        with self.assertRaises(HTTPException) as ctx:
            voice_routes.voice_command(
                _request(session), transcript="help",
                provider="text", csrf_token="bogus")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_meta_and_empty_are_honest_errors(self):
        import app.routes.voice as voice_routes

        session = auth.create_session(self.owner)
        self._start()
        _page, token = self._csrf(session)
        resp = voice_routes.voice_command(
            _request(session), transcript="hello", provider="meta",
            csrf_token=token)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not configured",
                      json.loads(resp.body.decode())["error"])
        resp = voice_routes.voice_command(
            _request(session), transcript="   ", provider="text",
            csrf_token=token)
        self.assertEqual(resp.status_code, 400)
