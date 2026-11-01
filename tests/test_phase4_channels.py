"""Phase 4 tests: messaging channels, webhooks, scheduling, outbox."""

import datetime as dt
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.channels import (
    ChannelError,
    ChannelNotConfigured,
    get_channel,
    parse_time_to_utc,
    resolve_recipient,
    verify_meta_signature,
)
from app.channels import outbox
from app.db import (
    SCHEMA_VERSION,
    driver_name,
    get_connection,
    init_db,
    insert_returning_id,
)


class ChannelCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "c4.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.owner = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email) VALUES (?, ?, ?)",
            ("Own", "member", "own4@example.org"))
        self.other = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email) VALUES (?, ?, ?)",
            ("Oth", "member", "oth4@example.org"))

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _account(self, channel="mock", owner=None, active=True):
        account_id = outbox.create_account(
            self.conn, self.owner if owner is None else owner,
            channel, f"{channel}-label",
            credentials={"bot_token": "t"} if channel == "telegram"
            else {})
        if active:
            outbox.activate_account(
                self.conn, account_id,
                self.owner if owner is None else owner)
        return account_id


class TestRegistry(unittest.TestCase):
    def test_all_channels_resolve(self):
        for name in ("telegram", "whatsapp", "messenger",
                     "instagram", "mock"):
            channel = get_channel(name)
            self.assertEqual(channel.name, name)
            self.assertTrue(channel.setup_requirements())
        with self.assertRaises(ChannelError):
            get_channel("pigeon")


class TestTelegram(unittest.TestCase):
    def test_send_needs_token_and_receipt(self):
        channel = get_channel("telegram")
        with self.assertRaises(ChannelNotConfigured):
            channel.send(None, {}, "123", "hi", "k")
        with self.assertRaises(ChannelError):
            channel.send(None, {"bot_token": "t"}, "123", "  ", "k")
        # Fake transport without ok:true refuses (no fake receipts).
        fake = lambda url, payload, headers: {"ok": False}
        with self.assertRaises(ChannelError):
            channel.send(fake, {"bot_token": "t"}, "123", "hi", "k")

        def good(url, payload, headers):
            self.assertIn("bottTEST/sendMessage", url)
            self.assertEqual(payload["chat_id"], "123")
            return {"ok": True, "result": {"message_id": 42}}

        receipt = channel.send(good, {"bot_token": "tTEST"}, "123",
                               "hi", "k")
        self.assertEqual(receipt.provider_msg_id, "42")

    def test_webhook_verify_and_parse(self):
        channel = get_channel("telegram")
        headers = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
        self.assertTrue(channel.verify_webhook("s3cret", b"{}", headers))
        self.assertFalse(channel.verify_webhook("s3cret", b"{}", {}))
        self.assertFalse(channel.verify_webhook("", b"{}", headers))
        body = json.dumps({
            "update_id": 7,
            "message": {"message_id": 9, "date": 1700000000,
                        "chat": {"id": 123},
                        "from": {"first_name": "Karma"},
                        "text": "Missed you too"}}).encode()
        (incoming,) = channel.parse_webhook(body)
        self.assertEqual(
            (incoming.external_id, incoming.sender_name, incoming.body),
            ("123", "Karma", "Missed you too"))
        self.assertEqual(channel.parse_webhook(b"nope"), [])
        self.assertEqual(channel.parse_webhook(b'{"a": 1}'), [])


class TestMetaChannels(unittest.TestCase):
    def _signed(self, secret, body):
        digest = hmac.new(secret.encode(), body,
                          hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": f"sha256={digest}"}

    def test_hmac_verify(self):
        body = b'{"object": "page"}'
        headers = self._signed("appsecret", body)
        for name in ("whatsapp", "messenger", "instagram"):
            channel = get_channel(name)
            self.assertTrue(
                channel.verify_webhook("appsecret", body, headers),
                name)
            self.assertFalse(
                channel.verify_webhook("appsecret", b"tampered",
                                       headers), name)
            self.assertFalse(
                channel.verify_webhook("appsecret", body, {}), name)

    def test_sends_refuse_with_setup(self):
        for name in ("whatsapp", "messenger", "instagram"):
            channel = get_channel(name)
            with self.assertRaises(ChannelNotConfigured):
                channel.send(None, {}, "1", "hi", "k")

    def test_whatsapp_parse(self):
        body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"messages": [
                {"from": "1555", "id": "w1", "timestamp": "1700000001",
                 "type": "text", "text": {"body": "Hello there"}},
                {"from": "1555", "id": "w2", "type": "image"}]}}]}]
        }).encode()
        (incoming,) = get_channel("whatsapp").parse_webhook(body)
        self.assertEqual((incoming.external_id, incoming.body),
                         ("1555", "Hello there"))

    def test_messenger_parse(self):
        body = json.dumps({
            "object": "page",
            "entry": [{"messaging": [
                {"sender": {"id": "777"},
                 "message": {"mid": "m1",
                             "text": "Hey twin"}}]}]}
        ).encode()
        (incoming,) = get_channel("messenger").parse_webhook(body)
        self.assertEqual((incoming.external_id, incoming.body),
                         ("777", "Hey twin"))


class TestScheduler(unittest.TestCase):
    def test_utc_and_offsets(self):
        now = dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(
            parse_time_to_utc("11 PM", "UTC", now), "2026-05-01 23:00:00")
        self.assertEqual(
            parse_time_to_utc("23:00", "UTC", now), "2026-05-01 23:00:00")
        # Past today -> tomorrow.
        self.assertEqual(
            parse_time_to_utc("9 AM", "UTC", now), "2026-05-02 09:00:00")
        # UTC+2 midnight local is 22:00 UTC the day before.
        self.assertEqual(
            parse_time_to_utc("12 AM", "UTC+2", now),
            "2026-05-01 22:00:00")

    def test_iana_zone(self):
        now = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)
        # London is UTC in January.
        self.assertEqual(
            parse_time_to_utc("11 PM", "Europe/London", now),
            "2026-01-10 23:00:00")
        with self.assertRaises(ChannelError):
            parse_time_to_utc("11 PM", "Mars/Olympus", now)
        with self.assertRaises(ChannelError):
            parse_time_to_utc("whenever", "UTC", now)

    def test_recipient_resolution(self):
        contacts = [{"id": 1, "display_name": "Karma"},
                    {"id": 2, "display_name": "Karma Jr"}]
        with self.assertRaisesRegex(ChannelError, "ambiguous"):
            resolve_recipient(contacts, "karm")
        self.assertEqual(resolve_recipient(contacts, "karma")["id"], 1)
        self.assertEqual(resolve_recipient(contacts, "Karma Jr")["id"], 2)
        with self.assertRaisesRegex(ChannelError, "No contact"):
            resolve_recipient(contacts, "Nobody")
        with self.assertRaises(ChannelError):
            resolve_recipient(contacts, "  ")


class TestOutbox(ChannelCase):
    def _contact(self, account_id, name="Karma", external="777"):
        return outbox.add_contact(self.conn, account_id, self.owner,
                                  external_id=external,
                                  display_name=name)

    def test_account_lifecycle(self):
        account_id = self._account("mock", active=False)
        self.assertIsNone(
            outbox.get_account(self.conn, account_id, self.other))
        outbox.activate_account(self.conn, account_id, self.owner)
        row = outbox.get_account(self.conn, account_id, self.owner)
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["webhook_secret"])
        outbox.revoke_account(self.conn, account_id, self.owner)
        row = outbox.get_account(self.conn, account_id, self.owner)
        self.assertEqual(
            (row["status"], row["credentials_json"],
             row["webhook_secret"]),
            ("revoked", "{}", ""))
        with self.assertRaises(ChannelError):
            outbox.create_account(self.conn, self.owner, "pigeon", "x")

    def test_draft_schedule_send_mock(self):
        account_id = self._account()
        contact_id = self._contact(account_id)
        msg_id = outbox.draft_message(
            self.conn, account_id, self.owner, contact_id=contact_id,
            body="I missed you")
        with self.assertRaisesRegex(ChannelError, "Confirm"):
            outbox.schedule_message(
                self.conn, msg_id, self.owner,
                scheduled_for_utc="2026-05-01 23:00:00",
                confirmed=False)
        outbox.schedule_message(
            self.conn, msg_id, self.owner,
            scheduled_for_utc="2026-05-01 23:00:00", confirmed=True)
        report = outbox.process_due(
            self.conn, "2026-05-01 23:00:01", post=None,
            account_id=account_id)
        self.assertEqual((report["sent"], report["failed"]), (1, 0))
        message = outbox.get_message(self.conn, msg_id, self.owner)
        self.assertEqual(message["status"], "sent")
        self.assertTrue(message["provider_msg_id"].startswith("mock-"))

    def test_send_now_delivers_mock_draft(self):
        account_id = self._account()
        contact_id = self._contact(account_id)
        msg_id = outbox.draft_message(
            self.conn, account_id, self.owner, contact_id=contact_id,
            body="Send me now")
        with self.assertRaisesRegex(ChannelError, "Confirm"):
            outbox.send_now(self.conn, msg_id, self.owner,
                            confirmed=False, post=None)
        outcome = outbox.send_now(self.conn, msg_id, self.owner,
                                  confirmed=True, post=None)
        self.assertEqual(outcome["status"], "sent")
        self.assertTrue(outcome["receipt"].startswith("mock-"))
        message = outbox.get_message(self.conn, msg_id, self.owner)
        self.assertEqual(message["status"], "sent")

    def test_duplicate_keys_never_double_send(self):
        account_id = self._account()
        contact_id = self._contact(account_id)
        first = outbox.draft_message(
            self.conn, account_id, self.owner, contact_id=contact_id,
            body="Hi", idempotency_key="once-only")
        with self.assertRaisesRegex(ChannelError, "Duplicate"):
            outbox.draft_message(
                self.conn, account_id, self.owner,
                contact_id=contact_id, body="Hi again",
                idempotency_key="once-only")
        outbox.schedule_message(
            self.conn, first, self.owner,
            scheduled_for_utc="2020-01-01 00:00:00", confirmed=True)
        outbox.process_due(self.conn, "2026-01-01 00:00:00", post=None,
                           account_id=account_id)
        outbox.process_due(self.conn, "2026-01-01 00:00:00", post=None,
                           account_id=account_id)
        message = outbox.get_message(self.conn, first, self.owner)
        self.assertEqual((message["status"], message["attempts"]),
                         ("sent", 1))

    def test_failure_retry_cancel(self):
        from app.channels.base import Channel

        class Down(Channel):
            name = "down"

            def send(self, *args, **kwargs):
                raise ChannelError("vendor down")

        account_id = self._account("mock")
        contact_id = self._contact(account_id)
        doomed = outbox.draft_message(
            self.conn, account_id, self.owner, contact_id=contact_id,
            body="Bye")
        outbox.schedule_message(
            self.conn, doomed, self.owner,
            scheduled_for_utc="2020-01-01 00:00:00", confirmed=True)
        with mock.patch("app.channels.outbox.get_channel",
                        return_value=Down()):
            report = outbox.process_due(
                self.conn, "2026-01-01 00:00:00", post=None,
                account_id=account_id)
        self.assertEqual(report["failed"], 1)
        message = outbox.get_message(self.conn, doomed, self.owner)
        self.assertEqual(message["status"], "failed")
        outbox.retry_message(self.conn, doomed, self.owner)
        outbox.cancel_message(self.conn, doomed, self.owner)
        message = outbox.get_message(self.conn, doomed, self.owner)
        self.assertEqual(message["status"], "cancelled")

    def test_revoked_and_pending_never_send(self):
        pending = self._account("mock", active=False)
        contact_id = outbox.add_contact(
            self.conn, pending, self.owner, external_id="1",
            display_name="Peer")
        msg_id = outbox.draft_message(
            self.conn, pending, self.owner, contact_id=contact_id,
            body="Hi")
        outbox.schedule_message(
            self.conn, msg_id, self.owner,
            scheduled_for_utc="2020-01-01 00:00:00", confirmed=True)
        report = outbox.process_due(self.conn, "2026-01-01 00:00:00",
                                    post=None, account_id=pending)
        self.assertEqual(report["attempted"], 0)
        message = outbox.get_message(self.conn, msg_id, self.owner)
        self.assertEqual(message["status"], "queued")

    def test_inbound_audited_and_contact_saved(self):
        from app.channels import IncomingMessage

        account_id = self._account()
        contact_id = outbox.log_inbound(
            self.conn, account_id,
            IncomingMessage(external_id="999", sender_name="Karma",
                            body="Hello"))
        self.assertIsNotNone(contact_id)
        again = outbox.log_inbound(
            self.conn, account_id,
            IncomingMessage(external_id="999", sender_name="Karma",
                            body="Again"))
        self.assertEqual(again, contact_id)
        self.assertEqual(
            len(outbox.list_contacts(self.conn, account_id)), 1)

    def test_auto_send_policy_skips_confirmation(self):
        account_id = self._account()
        contact_id = self._contact(account_id)
        outbox.set_permissions(self.conn, account_id, self.owner,
                               {"auto_send": True})
        msg_id = outbox.draft_message(
            self.conn, account_id, self.owner, contact_id=contact_id,
            body="Auto")
        outbox.schedule_message(
            self.conn, msg_id, self.owner,
            scheduled_for_utc="2020-01-01 00:00:00", confirmed=False)
        message = outbox.get_message(self.conn, msg_id, self.owner)
        self.assertEqual(message["status"], "queued")


class TestMigrationV11(ChannelCase):
    def test_messaging_tables_and_version(self):
        if driver_name(self.conn) == "postgres":
            rows = self.conn.execute(
                "SELECT table_name AS name FROM information_schema.tables"
                " WHERE table_schema = current_schema()")
            tables = {r["name"] for r in rows}
        else:
            rows = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = {r["name"] for r in rows}
        self.assertTrue({"channel_accounts", "channel_contacts",
                         "outbox_messages", "channel_events"} <= tables)
        version = self.conn.execute(
            "SELECT version FROM schema_version WHERE id = 1").fetchone()
        self.assertEqual(version["version"], SCHEMA_VERSION)


class TestChannelRouteWiring(unittest.TestCase):
    def test_post_handlers_guarded(self):
        from pathlib import Path as _Path

        text = (_Path(__file__).resolve().parent.parent
                / "app" / "routes" / "channels.py").read_text(
                    encoding="utf-8")
        handlers = ["channel_connect", "channel_activate",
                    "channel_revoke", "channel_add_contact",
                    "channel_draft", "channel_schedule",
                    "channel_send_now", "channel_cancel_message",
                    "channel_retry_message", "channel_process_due",
                    "channel_permissions", "channel_quick_schedule"]
        for name in handlers:
            start = text.find(f"def {name}(")
            self.assertGreaterEqual(start, 0, name)
            rest = text[start:]
            for marker in ("\n@router.", "\ndef "):
                cut = rest.find(marker, 1)
                if cut > 0:
                    rest = rest[:cut]
            self.assertIn("require_session_csrf", rest, name)
            self.assertIn("_limited", rest, name)
            if name != "channel_connect":
                self.assertIn("_own_account_or_404", rest, name)

    def test_webhook_verifies_before_parsing(self):
        from pathlib import Path as _Path

        text = (_Path(__file__).resolve().parent.parent
                / "app" / "routes" / "channels.py").read_text(
                    encoding="utf-8")
        start = text.find("def webhook_receive(")
        rest = text[start:]
        self.assertLess(rest.find("verify_webhook"),
                        rest.find("parse_webhook"))


if __name__ == "__main__":
    unittest.main()
