"""Universal Find Something flows (scenarios A-G).

Engine-level behavior (post visibility, reverse discovery,
bidirectional contact, voice posting) plus full-HTTP journeys for:
seeker-offer coordination (A), taxi coordination (B), arbitrary posts
(C), fresh-user parity (D), empty states (E), knowledge import (F),
and privacy/pause/withdrawal controls (G).
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from app import auth
from app import conversations as engine
from app import requests as need_engine
from app.conversations import ConversationError
from app.db import (
    create_opportunity,
    create_twin,
    create_user,
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)
from app.matching import find_needs, find_providers
from app.request_parse import parse_need
from app.requests import RequestError
from app.voice import commands
from app.voice.session import handle_transcript, reset_sessions

from tests.test_e2e_flows import E2ECase, _client, _hidden


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "flows.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        auth.reset_rate_limits()
        self._n = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _user(self, name, skills=(), location="Thimphu"):
        self._n += 1
        uid = create_user(
            self.conn, name=name, role="professional",
            email=f"{name.lower()}{self._n}@example.org",
            password_hash=auth.hash_password("flow-pass-99"))
        tid = create_twin(
            self.conn, user_id=uid, display_name=f"{name}Twin",
            persona="p", tone="t", avatar_seed=f"seed-{name}-{self._n}",
            authorized_fields=["skills", "location"],
            profile={"skills": list(skills), "experience_years": 3,
                     "location": location, "open_to_remote": True})
        self.conn.commit()
        return uid, tid

    def _need(self, uid, tid, text, category="other", key=None):
        parsed = parse_need(text)
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=tid, owner_user_id=uid,
            category=category, need_text=text,
            requirements=parsed["requirements"],
            idempotency_key=key or f"flow-{uuid.uuid4().hex}")
        return need_engine.confirm_request(self.conn, req_id, uid)


class TestUniversalPostModel(FlowCase):
    def test_shared_statuses(self):
        uid, tid = self._user("Amy")
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=tid, owner_user_id=uid,
            category="other", need_text="draft need",
            idempotency_key="vis-draft")
        draft = need_engine.get_request(self.conn, req_id)
        self.assertFalse(need_engine.is_shared(draft))
        opened = need_engine.confirm_request(self.conn, req_id, uid)
        self.assertTrue(need_engine.is_shared(opened))
        closed = need_engine.transition(self.conn, req_id, uid, "closed")
        self.assertFalse(need_engine.is_shared(closed))

    def test_request_title(self):
        self.assertEqual(
            need_engine.request_title("Build my website. It needs payments."),
            "Build my website")
        self.assertEqual(need_engine.request_title("  "), "(untitled request)")
        long = "x" * 200
        self.assertTrue(len(need_engine.request_title(long)) <= 80)

    def test_update_need_owner_edit(self):
        uid, tid = self._user("Bob")
        req = self._need(uid, tid, "Need a logo.")
        updated = need_engine.update_need(
            self.conn, req["id"], uid, need_text="Need a logo and brand kit.",
            category="service", requirements={"skills": ["design"]})
        self.assertEqual(updated["status"], "open")
        self.assertIn("brand kit", updated["need_text"])
        self.assertEqual(updated["category"], "service")

    def test_update_need_refusals(self):
        uid, tid = self._user("Cat")
        other, _ = self._user("Dan")
        req = self._need(uid, tid, "Need a tutor.")
        with self.assertRaises(RequestError):
            need_engine.update_need(self.conn, req["id"], other,
                                    need_text="hijack")
        with self.assertRaises(RequestError):
            need_engine.update_need(self.conn, req["id"], uid,
                                    need_text="   ")
        need_engine.transition(self.conn, req["id"], uid, "closed")
        with self.assertRaises(RequestError):
            need_engine.update_need(self.conn, req["id"], uid,
                                    need_text="too late")


class TestFindNeeds(FlowCase):
    def test_arbitrary_need_found_via_profile_words(self):
        seeker, seeker_twin = self._user("Eli")
        helper, helper_twin = self._user("Fay", skills=["website design"])
        self._need(seeker, seeker_twin,
                   "I'm looking for someone to help me build a website.")
        twin = get_twin_by_user(self.conn, helper)
        found = find_needs(self.conn, twin)
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertIn("website", item["need"]["need_text"])
        self.assertEqual(item["seeker"]["twin_display_name"], "EliTwin")
        self.assertIsNone(item["via_offering"])
        self.assertGreater(item["match"].score, 0)

    def test_needs_match_helper_offering(self):
        seeker, seeker_twin = self._user("Gus")
        helper, _ = self._user("Hal")
        create_opportunity(
            self.conn, owner_user_id=helper, kind="service",
            title="Logo design studio",
            description="Logos and brand kits for startups.",
            category="service")
        self.conn.commit()
        self._need(seeker, seeker_twin,
                   "Looking for someone to design a logo for my startup.")
        twin = get_twin_by_user(self.conn, helper)
        found = find_needs(self.conn, twin)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["via_offering"]["title"],
                         "Logo design studio")

    def test_private_and_own_needs_excluded(self):
        seeker, seeker_twin = self._user("Ivy")
        helper, _ = self._user("Jay", skills=["tutoring"])
        draft_id, _ = need_engine.create_need(
            self.conn, twin_id=seeker_twin, owner_user_id=seeker,
            category="other", need_text="tutoring draft never confirmed",
            idempotency_key="priv-draft")
        self.assertEqual(
            need_engine.get_request(self.conn, draft_id)["status"], "draft")
        req = self._need(helper, get_twin_by_user(
            self.conn, helper)["id"], "I need tutoring in math.")
        need_engine.transition(self.conn, req["id"], helper, "closed")
        twin = get_twin_by_user(self.conn, helper)
        self.assertEqual(find_needs(self.conn, twin), [])

    def test_budget_never_exposed(self):
        seeker, seeker_twin = self._user("Kay")
        helper, _ = self._user("Lia", skills=["driving"])
        text = "Need a taxi in Thimphu. My maximum budget is Nu. 500."
        parsed = parse_need(text)
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=seeker_twin, owner_user_id=seeker,
            category="transport", need_text=text,
            requirements=parsed["requirements"],
            idempotency_key="priv-budget")
        need_engine.confirm_request(self.conn, req_id, seeker)
        twin = get_twin_by_user(self.conn, helper)
        found = find_needs(self.conn, twin)
        self.assertEqual(len(found), 1)
        self.assertNotIn("budget_max", found[0]["need"]["requirements"])


class TestBidirectionalContact(FlowCase):
    def _pair(self):
        seeker, seeker_twin = self._user("Mia")
        helper, helper_twin = self._user("Ned")
        req = self._need(seeker, seeker_twin, "Need help moving house.")
        return seeker, seeker_twin, helper, helper_twin, req

    def test_provider_can_answer_discovered_need(self):
        seeker, seeker_twin, helper, helper_twin, req = self._pair()
        conv_id, created = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=helper_twin, twin_b_id=seeker_twin,
            created_by_user_id=helper, request_id=req["id"])
        self.assertTrue(created)
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertTrue(engine.is_participant_owner(
            self.conn, conv, seeker))
        self.assertTrue(engine.is_participant_owner(
            self.conn, conv, helper))

    def test_reverse_contact_reuses_same_chat(self):
        seeker, seeker_twin, helper, helper_twin, req = self._pair()
        first, created_first = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=helper_twin, twin_b_id=seeker_twin,
            created_by_user_id=helper, request_id=req["id"])
        second, created_second = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=seeker_twin, twin_b_id=helper_twin,
            created_by_user_id=seeker, request_id=req["id"])
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)

    def test_private_request_cannot_be_contacted(self):
        seeker, seeker_twin, helper, helper_twin, req = self._pair()
        need_engine.transition(self.conn, req["id"], seeker, "closed")
        with self.assertRaises(ConversationError):
            engine.create_conversation(
                self.conn, twin_a_id=helper_twin, twin_b_id=seeker_twin,
                created_by_user_id=helper, request_id=req["id"])

    def test_unrelated_request_rejected(self):
        seeker, seeker_twin, helper, helper_twin, req = self._pair()
        third, third_twin = self._user("Oli")
        with self.assertRaises(ConversationError):
            engine.create_conversation(
                self.conn, twin_a_id=helper_twin, twin_b_id=third_twin,
                created_by_user_id=helper, request_id=req["id"])

    def test_seeker_contact_records_offering(self):
        seeker, seeker_twin, helper, helper_twin, req = self._pair()
        opp_id = create_opportunity(
            self.conn, owner_user_id=helper, kind="service",
            title="Moving crew", category="service")
        self.conn.commit()
        conv_id, _ = engine.get_or_create_request_conversation(
            self.conn, twin_a_id=seeker_twin, twin_b_id=helper_twin,
            created_by_user_id=seeker, request_id=req["id"],
            opportunity_id=opp_id)
        conv = engine.get_conversation(self.conn, conv_id)
        self.assertEqual(conv["opportunity_id"], opp_id)
        self.assertEqual(conv["opp_title"], "Moving crew")


class TestVoiceNeedPosting(FlowCase):
    def setUp(self):
        super().setUp()
        reset_sessions()

    def tearDown(self):
        reset_sessions()
        super().tearDown()

    def test_post_and_looking_for_trigger_need(self):
        cmd = commands.parse_command(
            "Twin, post that I'm looking for a Java tutor.")
        self.assertEqual(cmd["kind"], "need")
        self.assertIn("Java tutor", cmd["slots"]["text"])
        self.assertNotIn("post that", cmd["slots"]["text"].lower())
        cmd = commands.parse_command("I'm looking for a taxi.")
        self.assertEqual(cmd["kind"], "need")
        cmd = commands.parse_command("I need a plumber.")
        self.assertEqual(cmd["kind"], "need")

    def test_voice_post_confirm_flow(self):
        uid, tid = self._user("Pam")
        first = handle_transcript(
            self.conn, user_id=uid, twin_id=tid,
            text="Twin, post that I'm looking for a Java tutor.",
            provider="text")
        self.assertIn("How long should I search", first["text"])
        second = handle_transcript(
            self.conn, user_id=uid, twin_id=tid, text="one week",
            provider="text")
        self.assertTrue(second["expect_confirmation"])
        self.assertIn("Post this request", second["text"])
        third = handle_transcript(
            self.conn, user_id=uid, twin_id=tid, text="yes",
            provider="text")
        self.assertIn("Posted request", third["text"])
        rows = list(self.conn.execute(
            "SELECT * FROM requests WHERE owner_user_id = ?", (uid,)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "open")
        self.assertIn("Java tutor", rows[0]["need_text"])
        self.assertTrue(rows[0]["expires_at"])

    def test_voice_stated_deadline_skips_question(self):
        uid, tid = self._user("Quinn")
        first = handle_transcript(
            self.conn, user_id=uid, twin_id=tid,
            text="Twin, post that I'm looking for a taxi,"
            " deadline two hours.",
            provider="text")
        self.assertTrue(first["expect_confirmation"])
        self.assertIn("Post this request", first["text"])

    def test_voice_no_deadline_accepted(self):
        uid, tid = self._user("Rita")
        handle_transcript(
            self.conn, user_id=uid, twin_id=tid,
            text="Twin, post that I'm looking for a plumber.",
            provider="text")
        second = handle_transcript(
            self.conn, user_id=uid, twin_id=tid, text="no deadline",
            provider="text")
        self.assertIn("Post this request", second["text"])
        third = handle_transcript(
            self.conn, user_id=uid, twin_id=tid, text="yes",
            provider="text")
        self.assertIn("Posted request", third["text"])
        rows = list(self.conn.execute(
            "SELECT * FROM requests WHERE owner_user_id = ?", (uid,)))
        self.assertIsNone(rows[0]["expires_at"])


class HTTPFlows(E2ECase):
    """Full-stack journeys through the real ASGI app (own cookie jars)."""

    def _login_client(self, name, email):
        client = _client()
        self.addCleanup(client.close)
        return self._register(client, name, email)

    def _post_offer(self, client, title, kind="job", skills="",
                    category="", description=""):
        page = client.get("/opportunities/new").text
        resp = client.post("/opportunities/new", data={
            "kind": kind, "title": title, "description": description,
            "skills": skills, "years": "", "location": "Thimphu",
            "remote_ok": "", "category": category,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/opportunities/", resp.headers["location"])

    def _post_need(self, client, text, category="other"):
        page = client.get("/requests/new").text
        resp = client.post("/requests/new", data={
            "need_text": text, "category": category,
            "idem_key": uuid.uuid4().hex,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers["location"]
        self.assertIn("/review", loc)
        req_id = loc.split("/")[2]
        review = client.get(f"/requests/{req_id}/review").text
        resp = client.post(f"/requests/{req_id}/confirm", data={
            "category": category, "budget": "", "origin": "",
            "destination": "", "when": "", "skills": "",
            "csrf_token": _hidden(review, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return req_id

    def _contact_first_provider(self, client, req_id):
        detail = client.get(f"/requests/{req_id}").text
        offering = _hidden(detail, "offering_id")
        resp = client.post(f"/requests/{req_id}/contact", data={
            "offering_id": offering,
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/conversations/", resp.headers["location"])
        return resp.headers["location"].split("/")[2]

    def _decide(self, client, conv_id, decision):
        page = client.get(f"/conversations/{conv_id}").text
        resp = client.post(f"/conversations/{conv_id}/decision", data={
            "decision": decision,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _publish(self, client, display, skills="", location=""):
        """Opt the twin into public listing (private by default)."""
        page = client.get("/twin/edit").text
        resp = client.post("/twin/edit", data={
            "display_name": display, "persona": "helpful",
            "tone": "friendly", "skills": skills, "location": location,
            "authorized": ["skills", "location"],
            "public_listing": "on",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _csrf_from(self, client, path):
        return _hidden(client.get(path).text, "csrf_token")


class TestScenarioA_Internship(HTTPFlows):
    def test_post_discover_coordinate_decide(self):
        karma = self._login_client("Karma", "karma.a@example.org")
        deepak = self._login_client("Deepak", "deepak.a@example.org")
        self._make_twin(karma, "Kay")
        self._make_twin(deepak, "Dee")
        self._publish(karma, "Kay")
        self._publish(deepak, "Dee")
        self._post_offer(karma, "Frontend Internship", kind="job",
                         skills="HTML, CSS", category="",
                         description="Three-month frontend internship.")
        req_id = self._post_need(
            deepak, "I'm looking for a frontend internship in Thimphu.")
        detail = deepak.get(f"/requests/{req_id}").text
        self.assertIn("Frontend Internship", detail)
        conv_id = self._contact_first_provider(deepak, req_id)
        # Both owners see the same coordination in their Requests page.
        for client in (karma, deepak):
            body = client.get("/requests").text
            self.assertIn(f"/conversations/{conv_id}", body)
            self.assertIn("Frontend Internship", body)
        # Independent decisions stay consistent on both sides.
        self._decide(deepak, conv_id, "interested")
        self._decide(karma, conv_id, "interested")
        for client in (karma, deepak):
            body = client.get("/requests").text
            self.assertIn("me: interested", body)
            self.assertIn("them: interested", body)
        self._decide(karma, conv_id, "not-interested")
        self.assertIn("them: not-interested",
                      deepak.get("/requests").text)
        self.assertIn("me: not-interested",
                      karma.get("/requests").text)


class TestScenarioB_Taxi(HTTPFlows):
    def test_taxi_offer_discovered_and_coordinated(self):
        tashi = self._login_client("Tashi", "tashi.b@example.org")
        rider = self._login_client("Rider", "rider.b@example.org")
        self._make_twin(tashi, "Tash")
        self._make_twin(rider, "Rid")
        self._publish(tashi, "Tash")
        self._publish(rider, "Rid")
        self._post_offer(tashi, "Airport transfers", kind="service",
                         category="transport",
                         description="Airport pickup and drop cab.")
        req_id = self._post_need(
            rider, "I need a taxi to the airport tomorrow.",
            category="transport")
        detail = rider.get(f"/requests/{req_id}").text
        self.assertIn("Airport transfers", detail)
        conv_id = self._contact_first_provider(rider, req_id)
        for client in (tashi, rider):
            self.assertIn(f"/conversations/{conv_id}",
                          client.get("/requests").text)


class TestScenarioC_ArbitraryPost(HTTPFlows):
    def test_custom_need_found_and_answered(self):
        poster = self._login_client("Ava", "ava.c@example.org")
        helper = self._login_client("Ben", "ben.c@example.org")
        self._make_twin(poster, "Av")
        self._make_twin(helper, "Be")
        self._publish(poster, "Av")
        self._publish(helper, "Be")
        self._post_offer(helper, "Weekend web dev help", kind="service",
                         skills="HTML, CSS, JavaScript", category="service",
                         description="I build small websites on weekends.")
        req_id = self._post_need(
            poster, "I'm looking for someone to help me build a website.")
        # The helper discovers the arbitrary need on Discover.
        discover = helper.get("/discover").text
        self.assertIn("Open needs you could help with", discover)
        self.assertIn("build a website", discover)
        self.assertIn(f"/discover/needs/{req_id}/contact", discover)
        # Provider-side contact reaches the same shared coordination.
        resp = helper.post(f"/discover/needs/{req_id}/contact", data={
            "csrf_token": _hidden(discover, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conv_id = resp.headers["location"].split("/")[2]
        for client in (poster, helper):
            body = client.get("/requests").text
            self.assertIn(f"/conversations/{conv_id}", body)
        # A repeated answer reuses the one chat (no duplicate).
        again = helper.post(f"/discover/needs/{req_id}/contact", data={
            "csrf_token": _hidden(helper.get("/discover").text,
                                  "csrf_token")},
            follow_redirects=False)
        self.assertIn(conv_id, again.headers["location"])


class TestScenarioD_FreshUser(HTTPFlows):
    def test_registered_user_full_lifecycle(self):
        nada = self._login_client("Nada", "nada.d@example.org")
        owen = self._login_client("Owen", "owen.d@example.org")
        self._make_twin(nada, "Na")
        self._make_twin(owen, "Ow")
        self._publish(nada, "Na")
        self._publish(owen, "Ow")
        # Post a custom need with no preconfigured category fit.
        req_id = self._post_need(
            nada, "I need a tutor for Java on weekday evenings.")
        self._post_offer(owen, "Java tutoring", kind="service",
                         skills="Java", category="service",
                         description="Evening Java lessons, remote.")
        # Discover, coordinate, step the chat, decide, withdraw.
        detail = nada.get(f"/requests/{req_id}").text
        self.assertIn("Java tutoring", detail)
        conv_id = self._contact_first_provider(nada, req_id)
        chat = nada.get(f"/conversations/{conv_id}").text
        step = nada.post(f"/conversations/{conv_id}/step", data={
            "csrf_token": _hidden(chat, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(step.status_code, 303)
        self._decide(nada, conv_id, "interested")
        self.assertIn("me: interested",
                      nada.get("/requests").text)
        # Edit then withdraw the post.
        edit_page = nada.get(f"/requests/{req_id}").text
        resp = nada.post(f"/requests/{req_id}/update", data={
            "need_text": "I need a tutor for Java on weekends.",
            "category": "service", "budget": "", "origin": "",
            "destination": "", "when": "", "skills": "Java",
            "csrf_token": _hidden(edit_page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("weekends", nada.get(f"/requests/{req_id}").text)
        resp = nada.post(f"/requests/{req_id}/close", data={
            "csrf_token": _hidden(nada.get(f"/requests/{req_id}").text,
                                  "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("closed", nada.get(f"/requests/{req_id}").text)
        # Withdrawn needs leave discovery.
        self.assertNotIn("Java tutor",
                         owen.get("/discover").text)


class TestScenarioE_EmptyStates(HTTPFlows):
    def test_fresh_user_pages_render(self):
        fresh = self._login_client("Fresh", "fresh.e@example.org")
        discover = fresh.get("/discover")
        self.assertEqual(discover.status_code, 200)
        self.assertIn("No open offers match", discover.text)
        requests = fresh.get("/requests")
        self.assertEqual(requests.status_code, 200)
        self.assertIn("No requests yet", requests.text)
        self.assertIn("No coordination yet", requests.text)

    def test_mixed_posts_render(self):
        owner = self._login_client("Owner", "owner.e@example.org")
        other = self._login_client("Other", "other.e@example.org")
        self._make_twin(owner, "Own")
        self._make_twin(other, "Oth")
        self._post_offer(owner, "Untitled gig", kind="other",
                         description="")
        self._post_offer(owner, "Remote design work", kind="service",
                         category="service", description="Logos.")
        self._post_need(other, "Looking for remote design work.")
        discover = other.get("/discover")
        self.assertEqual(discover.status_code, 200)
        self.assertIn("Remote design work", discover.text)
        self.assertIn("Untitled gig", discover.text)
        self.assertEqual(other.get("/requests").status_code, 200)


class TestScenarioF_KnowledgeImport(HTTPFlows):
    FIXTURE = (Path(__file__).resolve().parent / "fixtures"
               / "sample_export.json")

    def _import(self, client, filename, payload, content_type):
        page = client.get("/memories").text
        return client.post("/memories/import", data={
            "csrf_token": _hidden(page, "csrf_token")}, files={
            "export_file": (filename, payload, content_type)},
            follow_redirects=False)

    def test_valid_export_imports_for_review(self):
        user = self._login_client("Ivy", "ivy.f@example.org")
        self._make_twin(user, "Iv")
        resp = self._import(user, "conversations.json",
                            self.FIXTURE.read_bytes(), "application/json")
        self.assertEqual(resp.status_code, 303)
        notice = resp.headers["location"]
        self.assertIn("imported-2", notice)
        body = user.get("/memories").text
        self.assertIn("FastAPI for three years", body)
        self.assertIn("GraphQL", body)

    def test_invalid_file_rejected_without_claim(self):
        user = self._login_client("Jed", "jed.f@example.org")
        self._make_twin(user, "Je")
        resp = self._import(user, "notes.txt", b"just text",
                            "text/plain")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=", resp.headers["location"])
        self.assertNotIn("imported-", resp.headers["location"])

    def test_empty_export_reports_no_messages(self):
        user = self._login_client("Kim", "kim.f@example.org")
        self._make_twin(user, "Ki")
        resp = self._import(user, "conversations.json", b"[]",
                            "application/json")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=", resp.headers["location"])


class TestScenarioG_Controls(HTTPFlows):
    def test_cross_user_request_is_private(self):
        owner = self._login_client("Leo", "leo.g@example.org")
        stranger = self._login_client("Max", "max.g@example.org")
        self._make_twin(owner, "Le")
        self._make_twin(stranger, "Ma")
        req_id = self._post_need(owner, "Private business need.")
        self.assertEqual(
            stranger.get(f"/requests/{req_id}").status_code, 404)
        self.assertEqual(
            stranger.get(f"/requests/{req_id}/review").status_code, 404)
        # The stranger's own list never shows the private need.
        self.assertNotIn("Private business need",
                         stranger.get("/requests").text)

    def test_cannot_contact_own_need(self):
        user = self._login_client("Nia", "nia.g@example.org")
        self._make_twin(user, "Ni")
        req_id = self._post_need(user, "Need a ride across town.")
        resp = user.post(f"/discover/needs/{req_id}/contact", data={
            "csrf_token": self._csrf_from(user, "/requests/new")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_private_need_cannot_be_contacted(self):
        seeker = self._login_client("Ola", "ola.g@example.org")
        helper = self._login_client("Pat", "pat.g@example.org")
        self._make_twin(seeker, "Ol")
        self._make_twin(helper, "Pa")
        req_id = self._post_need(seeker, "Need a dog walker.")
        page = seeker.get(f"/requests/{req_id}").text
        seeker.post(f"/requests/{req_id}/close", data={
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        resp = helper.post(f"/discover/needs/{req_id}/contact", data={
            "csrf_token": self._csrf_from(helper, "/requests/new")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_pause_blocks_and_resume_restores(self):
        first = self._login_client("Ray", "ray.g@example.org")
        second = self._login_client("Sam", "sam.g@example.org")
        self._make_twin(first, "Ra")
        self._make_twin(second, "Sa")
        self._publish(first, "Ra")
        self._publish(second, "Sa")
        self._post_offer(second, "Bike repair", kind="service",
                         description="Fixes bikes.")
        req_id = self._post_need(first, "Need my bike fixed.")
        conv_id = self._contact_first_provider(first, req_id)
        page = first.get(f"/conversations/{conv_id}").text
        token = _hidden(page, "csrf_token")
        first.post(f"/conversations/{conv_id}/pause",
                   data={"csrf_token": token}, follow_redirects=False)
        chat = first.get(f"/conversations/{conv_id}").text
        self.assertNotIn("Next twin message", chat)
        before = chat.count("msg-body")
        first.post(f"/conversations/{conv_id}/step",
                   data={"csrf_token": _hidden(chat, "csrf_token")},
                   follow_redirects=False)
        after = first.get(f"/conversations/{conv_id}").text.count(
            "msg-body")
        self.assertEqual(before, after)
        first.post(f"/conversations/{conv_id}/resume",
                   data={"csrf_token": _hidden(
                       first.get(f"/conversations/{conv_id}").text,
                       "csrf_token")},
                   follow_redirects=False)
        chat = first.get(f"/conversations/{conv_id}").text
        self.assertIn("Next twin message", chat)

    def test_no_screening_language_in_flows(self):
        seeker = self._login_client("Tia", "tia.g@example.org")
        helper = self._login_client("Uma", "uma.g@example.org")
        self._make_twin(seeker, "Ti")
        self._make_twin(helper, "Um")
        self._publish(seeker, "Ti")
        self._publish(helper, "Um")
        self._post_offer(helper, "Frontend Internship", kind="job",
                         skills="HTML", description="Internship.")
        req_id = self._post_need(seeker, "Looking for a frontend internship.")
        conv_id = self._contact_first_provider(seeker, req_id)
        self._decide(seeker, conv_id, "interested")
        bodies = "\n".join([
            seeker.get("/discover").text,
            seeker.get("/requests").text,
            seeker.get(f"/requests/{req_id}").text,
            seeker.get(f"/conversations/{conv_id}").text,
            helper.get("/requests").text,
        ]).lower()
        for banned in ("suitability", "candidate score", "hiring verdict",
                       "recommended for hire", "not recommended",
                       "interview assessment", "shortlist"):
            self.assertNotIn(banned, bodies)
