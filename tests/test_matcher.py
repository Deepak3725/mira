"""Event-driven persistent-search matching: engine behavior."""

import datetime
import tempfile
import unittest
import uuid
from pathlib import Path

from app import auth
from app import matcher
from app import requests as need_engine
from app.db import (
    claim_due_match_events,
    create_opportunity,
    create_twin,
    create_user,
    enqueue_match_event,
    get_connection,
    get_match_by_pair,
    get_request,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    list_matches_for_search,
    list_notifications,
)
from app.request_parse import parse_need

T0 = "2026-01-01 00:00:00"


class MatcherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "matcher.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        auth.reset_rate_limits()
        self._n = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _user(self, name, public=True):
        self._n += 1
        uid = create_user(
            self.conn, name=name, role="professional",
            email=f"{name.lower()}{self._n}@example.org",
            password_hash=auth.hash_password("matcher-pass-99"))
        tid = create_twin(
            self.conn, user_id=uid, display_name=f"{name}Twin",
            persona="p", tone="t", avatar_seed=f"m-{name}-{self._n}",
            authorized_fields=["skills", "location"], is_public=public,
            profile={"skills": ["Python"], "experience_years": 3,
                     "location": "Thimphu", "open_to_remote": True})
        self.conn.commit()
        return uid, tid

    def _search(self, uid, tid, text, **kw):
        parsed = parse_need(text)
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=tid, owner_user_id=uid,
            category=kw.get("category", parsed["category"]),
            need_text=text, requirements=parsed["requirements"],
            expires_at=kw.get("expires_at"),
            quantity_requested=kw.get("quantity", 1),
            idempotency_key=kw.get("key") or f"m-{uuid.uuid4().hex}")
        return need_engine.confirm_request(self.conn, req_id, uid)

    def _offer(self, uid, title, **kw):
        opp_id = create_opportunity(
            self.conn, owner_user_id=uid, kind=kw.get("kind", "service"),
            title=title, description=kw.get("description", ""),
            category=kw.get("category", ""),
            required_skills=kw.get("skills", []),
            location=kw.get("location", ""))
        self.conn.commit()
        return opp_id


class TestDirection(unittest.TestCase):
    def test_demand_side(self):
        for text in ("Hi twin, find me a fullStack Developer job.",
                     "I need someone to teach me Java.",
                     "Looking for a vehicle to rent.",
                     "Need a taxi going from Thimphu to Dagana.",
                     "I need a website developer for my business."):
            self.assertEqual(matcher.need_direction(text), "demand",
                             text)

    def test_supply_side(self):
        for text in ("I am available to tutor Java.",
                     "My car is available for rent.",
                     "I am a web developer available for projects.",
                     "Find me four passengers going from Thimphu to Dagana.",
                     "Looking for riders to share fuel."):
            self.assertEqual(matcher.need_direction(text), "supply",
                             text)


class TestCompatible(MatcherCase):
    def test_same_side_never_matches(self):
        seeker, stwin = self._user("Amy")
        rider, rtwin = self._user("Bob")
        search = self._search(seeker, stwin, "Need a taxi to the airport.")
        other = self._search(rider, rtwin, "Need a taxi to the airport.")
        counter = matcher._counterpart_facts(self.conn, "request",
                                             other["id"])
        verdict = matcher.compatible({}, search["need_text"], "demand",
                                     counter)
        self.assertIsNone(verdict)

    def test_single_common_word_insufficient(self):
        seeker, stwin = self._user("Cat")
        helper, _ = self._user("Dan")
        search = self._search(seeker, stwin, "Need a taxi urgently.")
        opp_id = self._offer(helper, "Taxi fleet dispatch software",
                             description="Software for dispatchers.")
        counter = matcher._counterpart_facts(self.conn, "opportunity",
                                             opp_id)
        verdict = matcher.compatible({}, search["need_text"], "demand",
                                     counter)
        # Only "taxi" is shared: not a match.
        self.assertIsNone(verdict)

    def test_route_overlap_matches(self):
        seeker, stwin = self._user("Eli")
        driver, _ = self._user("Fay")
        search = self._search(
            seeker, stwin,
            "Need a taxi going from Thimphu to Dagana tomorrow.")
        opp_id = self._offer(
            driver, "Dagana transfers",
            description="Taxi from Thimphu to Dagana every morning.")
        counter = matcher._counterpart_facts(self.conn, "opportunity",
                                             opp_id)
        self.assertEqual(counter["requirements"].get("destination"),
                         "Dagana")
        verdict = matcher.compatible(
            need_engine.request_requirements(search), search["need_text"],
            "demand", counter)
        self.assertIsNotNone(verdict)
        self.assertIn("same route", verdict[1])

    def test_approved_twin_skills_reused(self):
        seeker, stwin = self._user("Ivy")
        helper, _ = self._user("Jay")
        # No shared words at all: only the twin's approved Python
        # skill (not stated in the text) can connect these two.
        search = self._search(seeker, stwin, "Looking for weekend gigs.")
        opp_id = self._offer(helper, "Python consultancy",
                             description="Python consultancy for teams.",
                             skills=["Python"])
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 1)

    def test_private_twin_skills_ignored(self):
        uid = create_user(
            self.conn, name="Kay", role="professional",
            email="kaypriv@example.org",
            password_hash=auth.hash_password("matcher-pass-99"))
        tid = create_twin(
            self.conn, user_id=uid, display_name="KayTwin",
            persona="p", tone="t", avatar_seed="m-kay-priv",
            authorized_fields=[], profile={"skills": ["Python"]})
        self.conn.commit()
        helper, _ = self._user("Lia")
        search = self._search(uid, tid, "Looking for weekend gigs.")
        opp_id = self._offer(helper, "Python consultancy",
                             description="Python consultancy for teams.",
                             skills=["Python"])
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 0)

    def test_reason_hides_budget_figures(self):
        seeker, stwin = self._user("Gus")
        driver, _ = self._user("Hal")
        search = self._search(
            seeker, stwin,
            "Need a taxi in Thimphu. My maximum budget is Nu. 500.")
        opp_id = self._offer(driver, "City cabs",
                             description="Taxi rides around Thimphu.")
        counter = matcher._counterpart_facts(self.conn, "opportunity",
                                             opp_id)
        verdict = matcher.compatible(
            need_engine.request_requirements(search), search["need_text"],
            "demand", counter)
        self.assertIsNotNone(verdict)
        self.assertNotIn("500", verdict[1])


class TestMatchLifecycle(MatcherCase):
    def test_save_triggers_match_and_notifies_both(self):
        seeker, stwin = self._user("Ivy")
        helper, _ = self._user("Jay")
        search = self._search(
            seeker, stwin, "I'm looking for someone to design a logo.")
        self.assertEqual(matcher.match_search(self.conn, search["id"]),
                         {"created": 0, "skipped": 0})
        opp_id = self._offer(helper, "Logo design studio",
                             description="We design logos for startups.")
        result = matcher.match_offer(self.conn, opp_id)
        self.assertEqual(result["created"], 1)
        rows = list_matches_for_search(self.conn, search["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "potential")
        self.assertEqual(len(list_notifications(self.conn, seeker)), 1)
        self.assertEqual(len(list_notifications(self.conn, helper)), 1)
        # Reprocessing is idempotent: no duplicates, no new pings.
        again = matcher.match_offer(self.conn, opp_id)
        self.assertEqual(again["created"], 0)
        self.assertEqual(len(list_matches_for_search(
            self.conn, search["id"])), 1)
        self.assertEqual(len(list_notifications(self.conn, seeker)), 1)

    def test_new_need_matches_waiting_search(self):
        driver, dtwin = self._user("Nia")
        search = self._search(
            driver, dtwin,
            "Find me passengers going from Thimphu to Dagana.")
        rider, rtwin = self._user("Ola")
        need = self._search(
            rider, rtwin, "Need a ride from Thimphu to Dagana tomorrow.")
        result = matcher.match_need_as_counterpart(self.conn, need["id"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(list_matches_for_search(
            self.conn, search["id"])), 1)

    def test_private_peer_never_automatches(self):
        seeker, stwin = self._user("Kay")
        hidden, _ = self._user("Lia", public=False)
        search = self._search(seeker, stwin, "Need a logo designer.")
        opp_id = self._offer(hidden, "Logo design studio",
                             description="We design logos for startups.")
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 0)
        self.assertEqual(list_matches_for_search(self.conn, search["id"]),
                         [])

    def test_rejected_pair_never_resurrects(self):
        seeker, stwin = self._user("Mia")
        helper, _ = self._user("Ned")
        search = self._search(
            seeker, stwin, "Need a logo designer for my startup.")
        opp_id = self._offer(helper, "Logo design studio",
                             description="We design logos for startups.")
        matcher.match_offer(self.conn, opp_id)
        match = list_matches_for_search(self.conn, search["id"])[0]
        matcher.reject_match(self.conn, match["id"], seeker)
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 0)
        self.assertEqual(
            get_match_by_pair(self.conn, search["id"], "opportunity",
                              opp_id)["status"], "rejected")


class TestQueueAndExpiry(MatcherCase):
    def test_queue_retry_then_dead(self):
        enqueue_match_event(self.conn, event_type="bogus",
                            ref_type="request", ref_id=1)
        self.conn.commit()
        for attempt in range(matcher.MAX_QUEUE_ATTEMPTS):
            now = matcher.plus_minutes(T0, attempt * 30)
            counts = matcher.process_due(self.conn, now)
            self.assertEqual(counts["failed"], 1)
        rows = claim_due_match_events(self.conn, "2999-01-01 00:00:00")
        # Exhausted: kept for inspection, never re-claimed.
        self.assertEqual(rows, [])
        row = self.conn.execute(
            "SELECT * FROM match_queue").fetchone()
        self.assertEqual(row["status"], "dead")
        self.assertIn("Unknown event", row["last_error"])

    def test_expiry_and_extend(self):
        seeker, stwin = self._user("Oli")
        search = self._search(seeker, stwin, "Need a plumber.",
                              expires_at="2026-01-01 00:00:00")
        self.assertEqual(matcher.expire_due(self.conn, "2025-12-31 23:59:59"),
                         0)
        self.assertEqual(get_request(self.conn, search["id"])["status"],
                         "open")
        self.assertEqual(matcher.expire_due(self.conn, "2026-01-01 00:00:01"),
                         1)
        expired = get_request(self.conn, search["id"])
        self.assertEqual(expired["status"], "expired")
        notes = list_notifications(self.conn, seeker)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "expired")
        # Paused/fulfilled/expiry leave discovery: not shared.
        self.assertFalse(need_engine.is_shared(expired))
        extended = matcher.extend_search(self.conn, search["id"], seeker,
                                         1440)
        self.assertEqual(extended["status"], "open")
        self.assertTrue(extended["expires_at"] > "2026-01-01 00:00:01")

    def test_no_deadline_never_expires(self):
        seeker, stwin = self._user("Pam")
        search = self._search(seeker, stwin, "Need a plumber.")
        self.assertIsNone(search["expires_at"])
        self.assertEqual(matcher.expire_due(self.conn, "2999-01-01 00:00:00"),
                         0)

    def test_sweeper_thread_expires_and_stops(self):
        import threading
        import time

        import app.main as main_mod
        from app import db as db_mod

        seeker, stwin = self._user("Quinn")
        search = self._search(seeker, stwin, "Need a plumber.",
                              expires_at="2020-01-01 00:00:00")
        real_connect = db_mod.get_connection
        db_mod.get_connection = lambda *a, **k: real_connect(self.db)
        stop = threading.Event()
        worker = threading.Thread(target=main_mod._sweep_forever,
                                  args=(stop, 0.05), daemon=True)
        try:
            worker.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                # Fresh read: new connection per check.
                check = real_connect(self.db)
                try:
                    status = check.execute(
                        "SELECT status FROM requests WHERE id = ?",
                        (search["id"],)).fetchone()["status"]
                finally:
                    check.close()
                if status == "expired":
                    break
                time.sleep(0.05)
            self.assertEqual(status, "expired")
        finally:
            db_mod.get_connection = real_connect
            stop.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_pause_leaves_matching(self):
        seeker, stwin = self._user("Quinn")
        helper, _ = self._user("Rita")
        search = self._search(
            seeker, stwin, "Need a logo designer for my startup.")
        need_engine.transition(self.conn, search["id"], seeker, "paused")
        opp_id = self._offer(helper, "Logo design studio",
                             description="We design logos for startups.")
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 0)
        need_engine.transition(self.conn, search["id"], seeker, "open")
        self.assertEqual(
            matcher.match_offer(self.conn, opp_id)["created"], 1)


class TestQuantities(MatcherCase):
    def _four_seats(self):
        pema, pema_twin = self._user("Pema")
        search = self._search(
            pema, pema_twin,
            "Find me four passengers going from Thimphu to Dagana.",
            quantity=4)
        self.assertEqual(search["quantity_requested"], 4)
        riders = []
        for name in ("Ria", "Sia", "Tia", "Uia"):
            uid, tid = self._user(name)
            other = self._search(
                uid, tid,
                "Need a ride from Thimphu to Dagana tomorrow.")
            riders.append((uid, other))
        return pema, search, riders

    def test_partial_then_fulfilled(self):
        pema, search, riders = self._four_seats()
        self.assertEqual(matcher.match_search(
            self.conn, search["id"])["created"], 4)
        rows = list_matches_for_search(self.conn, search["id"])
        self.assertEqual(len(rows), 4)
        matcher.accept_match(self.conn, rows[0]["id"], pema)
        numbers = matcher.fulfillment(self.conn, search["id"])
        self.assertEqual(numbers, {"requested": 4, "accepted": 1,
                                   "remaining": 3})
        partial = get_request(self.conn, search["id"])
        self.assertIn(partial["status"], need_engine.SHARED_STATUSES)
        self.assertTrue(need_engine.is_shared(partial))
        for row in rows[1:]:
            matcher.accept_match(self.conn, row["id"], pema)
        self.assertEqual(get_request(self.conn, search["id"])["status"],
                         "fulfilled")
        kinds = [n["kind"] for n in list_notifications(self.conn, pema)]
        self.assertIn("fulfilled", kinds)

    def test_reject_reopens_fulfilled(self):
        pema, search, riders = self._four_seats()
        matcher.match_search(self.conn, search["id"])
        rows = list_matches_for_search(self.conn, search["id"])
        for row in rows:
            matcher.accept_match(self.conn, row["id"], pema)
        self.assertEqual(get_request(self.conn, search["id"])["status"],
                         "fulfilled")
        first = get_match_by_pair(self.conn, search["id"], "request",
                                  riders[0][1]["id"])
        matcher.reject_match(self.conn, first["id"], riders[0][0])
        reopened = get_request(self.conn, search["id"])
        self.assertEqual(reopened["status"], "open")
        numbers = matcher.fulfillment(self.conn, search["id"])
        self.assertEqual(numbers["accepted"], 3)

    def test_peer_accept_does_not_count(self):
        pema, search, riders = self._four_seats()
        matcher.match_search(self.conn, search["id"])
        row = get_match_by_pair(self.conn, search["id"], "request",
                                riders[0][1]["id"])
        matcher.accept_match(self.conn, row["id"], riders[0][0])
        numbers = matcher.fulfillment(self.conn, search["id"])
        self.assertEqual(numbers["accepted"], 0)
        updated = get_match_by_pair(self.conn, search["id"], "request",
                                    riders[0][1]["id"])
        self.assertEqual(updated["peer_accepted"], 1)
        self.assertEqual(updated["status"], "potential")

    def test_close_withdraws_and_notifies(self):
        pema, search, riders = self._four_seats()
        matcher.match_search(self.conn, search["id"])
        first = get_match_by_pair(self.conn, search["id"], "request",
                                  riders[0][1]["id"])
        matcher.accept_match(self.conn, first["id"], pema)
        need_engine.transition(self.conn, search["id"], pema, "closed")
        matcher.withdraw_search_matches(self.conn, search["id"])
        statuses = [m["status"] for m in
                    list_matches_for_search(self.conn, search["id"])]
        self.assertTrue(all(s == "withdrawn" for s in statuses))
        peer_notes = list_notifications(self.conn, riders[0][0])
        kinds = [n["kind"] for n in peer_notes]
        self.assertIn("withdrawn", kinds)
        # Potentials (never accepted) withdraw silently for the peer.
        self.assertNotIn("withdrawn",
                         [n["kind"] for n in
                          list_notifications(self.conn, riders[1][0])])

    def test_accept_starts_bounded_chat(self):
        seeker, stwin = self._user("Vera")
        helper, _ = self._user("Walt")
        search = self._search(
            seeker, stwin, "Need a logo designer for my startup.")
        opp_id = self._offer(helper, "Logo design studio",
                             description="We design logos for startups.")
        matcher.match_offer(self.conn, opp_id)
        row = list_matches_for_search(self.conn, search["id"])[0]
        matcher.accept_match(self.conn, row["id"], seeker)
        chats = list(self.conn.execute(
            "SELECT * FROM conversations WHERE request_id = ?",
            (search["id"],)))
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["opportunity_id"], opp_id)
        # Accepting again (same side) reuses the one chat.
        matcher.accept_match(self.conn, row["id"], seeker)
        chats = list(self.conn.execute(
            "SELECT * FROM conversations WHERE request_id = ?",
            (search["id"],)))
        self.assertEqual(len(chats), 1)


if __name__ == "__main__":
    unittest.main()
