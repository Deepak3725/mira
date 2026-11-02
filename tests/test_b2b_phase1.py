"""Bot-to-bot Phase 1 tests: requests, provider matching, gates, privacy."""

import datetime
import tempfile
import unittest
from pathlib import Path

from app import conversations as engine
from app import requests as need_engine
from app.conversations import ConversationError, TwinContext
from app.db import (
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    set_twin_conditions,
)
from app.matching import (
    find_providers,
    provider_summary_for_peer,
    score_provider,
)
from app.mock_mind import generate as mock_generate
from app.request_parse import parse_need
from app.requests import RequestError
from app.seed import seed_demo_data, seed_demo_drivers, seed_demo_opportunities


TAXI_NEED = ("I need a taxi from GCIT to Thimphu tomorrow at 9 AM. "
             "Find an available driver nearby. My maximum budget is Nu. 500.")


class B2BCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "b2b.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        seed_demo_drivers(self.db)
        self.conn = get_connection(self.db)
        self.deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        self.dorji = get_user_by_email(self.conn, "dorji.demo@example.com")
        self.sonam = get_user_by_email(self.conn, "sonam.demo@example.com")
        self.twin_d = get_twin_by_user(self.conn, self.deepak["id"])
        self.twin_o = get_twin_by_user(self.conn, self.dorji["id"])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _taxi_request(self, owner=None, twin=None, key="taxi-1"):
        owner = owner or self.deepak
        twin = twin or self.twin_d
        parsed = parse_need(TAXI_NEED)
        req_id, created = need_engine.create_need(
            self.conn, twin_id=twin["id"], owner_user_id=owner["id"],
            category=parsed["category"], need_text=TAXI_NEED,
            requirements=parsed["requirements"], idempotency_key=key)
        self.assertTrue(created)
        return need_engine.confirm_request(self.conn, req_id, owner["id"])


class TestParser(unittest.TestCase):
    def test_taxi_example_parses(self):
        out = parse_need(TAXI_NEED)
        self.assertEqual(out["category"], "transport")
        req = out["requirements"]
        self.assertEqual(req["budget_max"], 500.0)
        self.assertEqual(req["origin"], "GCIT")
        self.assertEqual(req["destination"], "Thimphu")
        self.assertEqual(req["when_text"], "tomorrow at 09:00")
        expected_day = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(req["when_iso"], expected_day + "T09:00")
        self.assertNotIn("skills", req)

    def test_partial_never_hallucinates(self):
        out = parse_need("I need help moving house")
        self.assertEqual(out["requirements"], {})
        self.assertTrue(out["warnings"])

    def test_bare_am_is_not_a_time(self):
        # "I am ..." must not store when_text="am".
        out = parse_need(
            "I'm looking for a full-stack developer job in Bhutan.")
        self.assertNotIn("when_text", out["requirements"])
        self.assertNotIn("when_iso", out["requirements"])
        out = parse_need(
            "I am looking for a full-stack developer job in Bhutan.")
        self.assertNotIn("when_text", out["requirements"])
        self.assertNotIn("when_iso", out["requirements"])

    def test_clock_time_with_meridiem_kept(self):
        out = parse_need("Pick me up tomorrow at 9:30pm")
        self.assertEqual(out["requirements"]["when_text"],
                         "tomorrow at 21:30")

    def test_quantity_words_are_not_skills(self):
        self.assertNotIn("skills",
                         parse_need("I need four seats")["requirements"])
        self.assertNotIn(
            "skills",
            parse_need("I need a taxi with four seats")["requirements"])

    def test_sentence_case_places_parse(self):
        req = parse_need("From Thimphu to Dagana.")["requirements"]
        self.assertEqual(req["origin"], "Thimphu")
        self.assertEqual(req["destination"], "Dagana")
        req = parse_need("I need a ride from here to there.")[
            "requirements"]
        self.assertNotIn("origin", req)
        self.assertNotIn("destination", req)

    def test_for_origin_and_learn_skill_parse(self):
        req = parse_need(
            "Taxi available for Thimphu to Dagana tomorrow.")[
            "requirements"]
        self.assertEqual(req["origin"], "Thimphu")
        self.assertEqual(req["destination"], "Dagana")
        req = parse_need("I want to learn backend development.")[
            "requirements"]
        self.assertEqual(req["skills"], ["backend"])
        # "learning" inside a hyphenated compound is not a trigger.
        req = parse_need("Find a machine-learning tutor.")[
            "requirements"]
        self.assertNotIn("skills", req)

    def test_with_skills_still_parsed(self):
        skills = parse_need(
            "Looking for someone with JavaScript, Node.js")["requirements"]["skills"]
        self.assertIn("JavaScript", skills)
        self.assertIn("Node.js", skills)

    def test_empty_and_deterministic(self):
        self.assertTrue(parse_need("")["warnings"])
        self.assertEqual(parse_need(TAXI_NEED), parse_need(TAXI_NEED))


class TestLifecycle(B2BCase):
    def test_create_confirm_close(self):
        parsed = parse_need(TAXI_NEED)
        req_id, created = need_engine.create_need(
            self.conn, twin_id=self.twin_d["id"], owner_user_id=self.deepak["id"],
            category="transport", need_text=TAXI_NEED,
            requirements=parsed["requirements"], idempotency_key="k-1")
        self.assertTrue(created)
        req = need_engine.confirm_request(self.conn, req_id, self.deepak["id"])
        self.assertEqual(req["status"], "open")
        with self.assertRaises(RequestError):
            need_engine.transition(self.conn, req_id, self.deepak["id"], "chatting")
        need_engine.transition(self.conn, req_id, self.deepak["id"], "matched")
        need_engine.transition(self.conn, req_id, self.deepak["id"], "chatting")
        need_engine.transition(self.conn, req_id, self.deepak["id"], "decided")
        need_engine.transition(self.conn, req_id, self.deepak["id"], "closed")
        with self.assertRaises(RequestError):
            need_engine.transition(self.conn, req_id, self.deepak["id"], "open")

    def test_owner_only_and_validation(self):
        with self.assertRaises(RequestError):
            need_engine.create_need(
                self.conn, twin_id=self.twin_d["id"],
                owner_user_id=self.dorji["id"], category="transport",
                need_text="hi", idempotency_key="x")
        with self.assertRaises(RequestError):
            need_engine.create_need(
                self.conn, twin_id=self.twin_d["id"],
                owner_user_id=self.deepak["id"], category="transport",
                need_text="  ", idempotency_key="y")
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=self.twin_d["id"], owner_user_id=self.deepak["id"],
            category="transport", need_text="need a ride", idempotency_key="z")
        with self.assertRaises(RequestError):
            need_engine.confirm_request(self.conn, req_id, self.dorji["id"])
        with self.assertRaises(RequestError):
            need_engine.transition(self.conn, req_id, self.dorji["id"], "closed")


class TestIdempotency(B2BCase):
    def test_duplicate_submission_returns_existing(self):
        kwargs = dict(twin_id=self.twin_d["id"], owner_user_id=self.deepak["id"],
                      category="transport", need_text=TAXI_NEED,
                      idempotency_key="same-key")
        first, created_first = need_engine.create_need(self.conn, **kwargs)
        second, created_second = need_engine.create_need(self.conn, **kwargs)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)
        rows = list(self.conn.execute(
            "SELECT id FROM requests WHERE owner_user_id = ?", (self.deepak["id"],)))
        self.assertEqual(len(rows), 1)

    def test_contact_twice_reuses_conversation(self):
        req = self._taxi_request()
        kwargs = dict(twin_a_id=self.twin_d["id"], twin_b_id=self.twin_o["id"],
                      created_by_user_id=self.deepak["id"], request_id=req["id"])
        first, created_first = engine.get_or_create_request_conversation(self.conn, **kwargs)
        second, created_second = engine.get_or_create_request_conversation(self.conn, **kwargs)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)
        rows = list(self.conn.execute("SELECT id FROM conversations"))
        self.assertEqual(len(rows), 1)  # exactly one chat, no duplicate
        actions = [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE conv_id = ?", (first,))]
        self.assertIn("contact-reused", actions)


class TestProviderMatching(B2BCase):
    def test_taxi_shortlist(self):
        req = self._taxi_request()
        shortlist = find_providers(self.conn, req)
        titles = [s["offering"]["title"] for s in shortlist]
        self.assertIn("City Taxi (Thimphu valley)", titles)
        self.assertNotIn("Premium Taxi (long trips)", titles)  # 650 > 500
        # Universal discovery: category is a label, not a gate, so the
        # job posting still appears — ranked below the fitting taxi.
        self.assertIn("Backend Developer", titles)
        self.assertLess(titles.index("City Taxi (Thimphu valley)"),
                        titles.index("Backend Developer"))
        top = shortlist[0]
        self.assertEqual(top["offering"]["title"], "City Taxi (Thimphu valley)")
        self.assertEqual(top["match"].score, 85)
        self.assertIn("Within budget", top["match"].explanation)

    def test_over_budget_excluded_with_reason(self):
        match = score_provider({"budget_max": 100}, {"price": 400})
        self.assertTrue(match.excluded_reason)
        self.assertEqual(match.score, 0)

    def test_unknowns_are_neutral_not_zero(self):
        match = score_provider({}, {})
        # Neutral halves: 20 + 15 + 7.5 + 15 = 57.5 -> 58.
        self.assertEqual(match.score, 58)
        self.assertGreater(match.score, 0)

    def test_deterministic_and_capped(self):
        req = self._taxi_request()
        first = find_providers(self.conn, req)
        second = find_providers(self.conn, req)
        self.assertEqual([s["offering"]["id"] for s in first],
                         [s["offering"]["id"] for s in second])
        self.assertEqual(find_providers(self.conn, req, limit=1).__len__(), 1)

    def test_own_offerings_excluded(self):
        from app.db import create_opportunity

        create_opportunity(
            self.conn, owner_user_id=self.deepak["id"], kind="gig",
            category="transport", title="Deepak's own ride",
            attributes={"price": 10, "service_area": ["GCIT", "Thimphu"]})
        self.conn.commit()
        req = self._taxi_request(key="own-1")
        titles = [s["offering"]["title"] for s in find_providers(self.conn, req)]
        self.assertNotIn("Deepak's own ride", titles)


class TestConditionAwareMock(unittest.TestCase):
    def _ctx(self, **kwargs):
        base = {"twin_name": "Dor", "owner_name": "Dorji", "persona": "driver",
                "tone": "warm", "public_view": {}, "peer_name": "Dee"}
        base.update(kwargs)
        return TwinContext(**base)

    def test_quotes_authorized_rate(self):
        ctx = self._ctx(public_view={"rate": "Nu. 400 per city trip"},
                        history=[("peer", "What is your rate?")])
        self.assertIn("Nu. 400", mock_generate(ctx))

    def test_deflects_unlisted_rate_and_budget(self):
        ctx = self._ctx(public_view={"skills": ["driving"]},
                        history=[("peer", "How much do you charge?")])
        self.assertIn("hasn't shared a rate", mock_generate(ctx))
        ctx = self._ctx(public_view={"skills": ["driving"]},
                        history=[("peer", "What is your maximum budget?")])
        self.assertIn("hasn't authorized", mock_generate(ctx))

    def test_intro_states_need_without_budget(self):
        ctx = self._ctx(
            public_view={"skills": ["driving"]}, history=[],
            request_summary={"category": "transport", "origin": "GCIT",
                             "destination": "Thimphu", "when_text": "tomorrow",
                             "budget_max": 500})
        reply = mock_generate(ctx)
        self.assertIn("GCIT to Thimphu", reply)
        self.assertNotIn("500", reply)


class TestApprovalGates(B2BCase):
    def _service_conv(self):
        req = self._taxi_request()
        conv_id, _ = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=self.twin_d["id"], twin_b_id=self.twin_o["id"],
            created_by_user_id=self.deepak["id"], request_id=req["id"])
        return conv_id

    def test_auto_answer_off_needs_owner_note(self):
        set_twin_conditions(self.conn, self.twin_d["id"], {"auto_answer": False})
        self.conn.commit()
        conv_id = self._service_conv()
        with self.assertRaises(ConversationError):
            engine.step_conversation(self.conn, conv_id, self.deepak["id"])
        engine.owner_note(self.conn, conv_id, self.deepak["id"], "Introduce us briefly.")
        result = engine.step_conversation(self.conn, conv_id, self.deepak["id"])
        self.assertEqual(result["message"]["sender"], "a")
        # Provider side still automatic.
        result = engine.step_conversation(self.conn, conv_id, self.dorji["id"])
        self.assertEqual(result["message"]["sender"], "b")

    def test_require_approval_flag(self):
        conv_id = self._service_conv()
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertFalse(engine.conversation_requires_approval(self.conn, conv))
        set_twin_conditions(self.conn, self.twin_o["id"], {"require_approval": True})
        self.conn.commit()
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertTrue(engine.conversation_requires_approval(self.conn, conv))


class TestBidirectionalPrivacy(B2BCase):
    SEEKER_SECRETS = ("17-000-001", "deepak.private", "Fictional Lane", "45,000")
    PROVIDER_SECRETS = ("17-000-005", "dorji.private")

    def _exchange(self):
        req = self._taxi_request()
        conv_id, _ = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=self.twin_d["id"], twin_b_id=self.twin_o["id"],
            created_by_user_id=self.deepak["id"], request_id=req["id"])
        for i in range(6):
            uid = self.deepak["id"] if i % 2 == 0 else self.dorji["id"]
            engine.step_conversation(self.conn, conv_id, uid)
        return req, conv_id

    def test_provider_context_has_no_budget(self):
        req, conv_id = self._exchange()
        conv = engine.get_conversation(self.conn, conv_id)
        provider_ctx = engine.build_context(self.conn, conv, "b")
        self.assertNotIn("budget_max", provider_ctx.request_summary)
        self.assertEqual(provider_ctx.request_summary["origin"], "GCIT")
        seeker_ctx = engine.build_context(self.conn, conv, "a")
        self.assertEqual(seeker_ctx.request_summary["budget_max"], 500.0)

    def test_shortlist_leaks_neither_side(self):
        req = self._taxi_request()
        blob = repr(find_providers(self.conn, req))
        for secret in self.SEEKER_SECRETS + self.PROVIDER_SECRETS:
            self.assertNotIn(secret, blob)

    def test_messages_and_audit_leak_neither_side(self):
        req, conv_id = self._exchange()
        bodies = "\n".join(m["body"] for m in engine.list_messages(self.conn, conv_id))
        details = "\n".join(r["detail"] for r in self.conn.execute(
            "SELECT detail FROM audit_events WHERE conv_id = ?", (conv_id,)))
        for secret in self.SEEKER_SECRETS + self.PROVIDER_SECRETS + ("500",):
            self.assertNotIn(secret, bodies)
            self.assertNotIn(secret, details)

    def test_summary_strips_budget(self):
        summary = provider_summary_for_peer({"origin": "GCIT", "budget_max": 500})
        self.assertEqual(summary, {"origin": "GCIT"})


class TestNoCommitment(B2BCase):
    def test_twins_cannot_decide_or_pay(self):
        req = self._taxi_request()
        conv_id, _ = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=self.twin_d["id"], twin_b_id=self.twin_o["id"],
            created_by_user_id=self.deepak["id"], request_id=req["id"])
        for i in range(4):
            uid = self.deepak["id"] if i % 2 == 0 else self.dorji["id"]
            engine.step_conversation(self.conn, conv_id, uid)
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertIsNone(conv["decision_a"])
        self.assertIsNone(conv["decision_b"])
        text = engine.generate_service_summary(self.conn, conv_id)
        self.assertIn("cannot commit", text)
        engine.set_decision(self.conn, conv_id, self.deepak["id"], "interested")
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertEqual(conv["decision_a"], "interested")


class TestTaxiEndToEnd(B2BCase):
    def test_full_mock_flow_at_zero_cost(self):
        parsed = parse_need(TAXI_NEED)
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=self.twin_d["id"], owner_user_id=self.deepak["id"],
            category=parsed["category"], need_text=TAXI_NEED,
            requirements=parsed["requirements"], idempotency_key="e2e-1")
        req = need_engine.confirm_request(self.conn, req_id, self.deepak["id"])
        self.assertEqual(req["status"], "open")
        shortlist = find_providers(self.conn, req)
        self.assertTrue(shortlist)
        conv_id, created = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=self.twin_d["id"], twin_b_id=self.twin_o["id"],
            created_by_user_id=self.deepak["id"], request_id=req_id)
        self.assertTrue(created)
        for i in range(4):
            uid = self.deepak["id"] if i % 2 == 0 else self.dorji["id"]
            engine.step_conversation(self.conn, conv_id, uid)
        engine.set_decision(self.conn, conv_id, self.deepak["id"], "interested")
        rows = list(self.conn.execute("SELECT DISTINCT provider FROM usage_log"))
        self.assertTrue(rows)
        self.assertEqual({r["provider"] for r in rows}, {"mock"})
        cost = self.conn.execute("SELECT SUM(est_cost_usd) AS t FROM usage_log").fetchone()["t"]
        self.assertEqual(cost, 0.0)


class TestDriverSeed(B2BCase):
    def test_drivers_idempotent(self):
        again = seed_demo_drivers(self.db)
        self.assertEqual(again["created"], 0)
        self.assertGreater(again["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
