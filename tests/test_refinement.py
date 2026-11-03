"""MIRA refinement suite: simplification + behavior improvements.

Covers the refinement deliverables through route handlers and engines
on an isolated temp database (no sockets, no network, mock provider
only): registration without the role question, twin roles, the twin
skills single source of truth, NL parsing (jobs, places, salary),
matching gates (category, salary, expiry, peer collaboration), mutual
acceptance with contact gating, simplified pages, voice removal, and
the teach/approve memory flow.
"""

import os
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app import auth
from app import config as config_mod
from app import db as db_mod
from app import findchat
from app import matcher
from app import posting
from app import requests as need_engine
from app.db import (
    add_twin_skills,
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    normalize_skill_list,
)
from app.intent import interpret_need
from app.matching import find_matches, find_providers, score_provider
from app.posting import classify_posting
from app.request_parse import CATEGORIES, parse_need
from app.routes import accounts as accounts_route
from app.routes import chats as chats_route
from app.routes import memories as memories_route
from app.routes import pages as pages_route
from app.routes import requests as requests_route
from app.routes import twin as twin_route
from tests.test_twin_chat import TwinChatCase


def _request(path="/", cookie=None, method="GET"):
    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
        "query_string": b"", "headers": headers,
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })


class RefineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "refine.db")
        init_db(self.db)
        auth.reset_rate_limits()
        self._saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db
        self._saved_settings = db_mod.settings
        db_mod.settings = config_mod.get_settings()
        self.accts = {}
        self._n = 0

    def tearDown(self):
        db_mod.settings = self._saved_settings
        if self._saved_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._saved_env
        self.tmp.cleanup()

    def _conn(self):
        return get_connection(self.db)

    def _register(self, name, email):
        resp = accounts_route.register(
            _request("/register", method="POST"), name=name, email=email,
            password="refine-pass-99", csrf_token=auth.csrf_token(None))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            uid = get_user_by_email(conn, email)["id"]
        finally:
            conn.close()
        resp = accounts_route.login(
            _request("/login", method="POST"), email=email,
            password="refine-pass-99",
            csrf_token=auth.csrf_token(None))
        self.assertEqual(resp.status_code, 303)
        raw = resp.headers["set-cookie"].split(";")[0]
        _, _, token = raw.partition("=")
        self.accts[email] = {
            "uid": uid, "cookie": f"{auth.SESSION_COOKIE}={token}"}
        return uid

    def _authed(self, email, path="/me", method="GET"):
        return _request(path, self.accts[email]["cookie"], method)

    def _csrf(self, email):
        return auth.csrf_for_request(self._authed(email),
                                     self.accts[email]["uid"])

    def _make_twin(self, email, display, skills="", location="",
                   public=True, twin_role="", phone="", email_addr="",
                   authorized=("skills", "location")):
        resp = accounts_route.twin_create(
            self._authed(email, "/twin/new", "POST"),
            display_name=display, persona="helpful", tone="friendly",
            headline="", skills=skills, experience_years="",
            location=location, availability="", portfolio_url="", bio="",
            company="", role_hiring="", requirements="", phone=phone,
            personal_email=email_addr, interests="", twin_role=twin_role,
            open_to_remote=None, authorized=list(authorized),
            public_listing="yes" if public else None,
            cond_max_price="", cond_auto_answer="yes",
            cond_require_approval=None, cond_distance="",
            cond_availability="", csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            twin = get_twin_by_user(conn, self.accts[email]["uid"])
        finally:
            conn.close()
        self.assertIsNotNone(twin)
        return twin["id"]

    def _body(self, resp):
        return resp.body.decode("utf-8")


class TestRegistrationAndTwin(RefineCase):
    def test_register_form_has_no_role_question(self):
        resp = accounts_route.register_form(_request("/register"))
        body = self._body(resp)
        self.assertNotIn("I am a", body)
        self.assertNotIn('name="role"', body)
        self.assertIn('name="name"', body)
        self.assertIn('name="password"', body)

    def test_register_without_role_still_works(self):
        uid = self._register("Rin", "rin.ref@example.org")
        conn = self._conn()
        try:
            user = get_user_by_email(conn, "rin.ref@example.org")
            self.assertEqual(user["id"], uid)
            self.assertEqual(user["role"], "professional")
        finally:
            conn.close()

    def test_twin_role_saved_and_shared(self):
        from app.privacy import twin_public_view

        email = "tashi.ref@example.org"
        self._register("Tashi", email)
        tid = self._make_twin(email, "Tas", twin_role="job_seeker",
                              authorized=("role", "skills"))
        conn = self._conn()
        try:
            from app.db import get_twin, twin_profile

            twin = get_twin(conn, tid)
            self.assertEqual(twin_profile(twin)["role"], "job_seeker")
            self.assertEqual(twin_public_view(twin)["role"], "job_seeker")
        finally:
            conn.close()

    def test_twin_form_offers_roles(self):
        email = "dorji.ref@example.org"
        self._register("Dorji", email)
        resp = accounts_route.twin_new_form(self._authed(email, "/twin/new"))
        body = self._body(resp)
        self.assertIn('name="twin_role"', body)
        self.assertIn("Job Seeker", body)
        self.assertIn("Collaboration", body)

    def test_dashboard_focuses_on_own_twin(self):
        email = "pema.ref@example.org"
        other = "karma.ref@example.org"
        self._register("Pema", email)
        self._register("Karma", other)
        self._make_twin(email, "Pem")
        self._make_twin(other, "Kay")
        resp = pages_route.dashboard(self._authed(email, "/dashboard"))
        body = self._body(resp)
        self.assertIn("Pem", body)
        self.assertNotIn("Kay", body)
        self.assertNotIn("Public twins", body)
        self.assertIn("Edit Twin", body)
        self.assertIn("Update Memory", body)
        self.assertIn('href="/twin/edit"', body)
        self.assertIn('href="/memories"', body)

    def test_me_journey_has_create_update_teach_only(self):
        email = "sonam.ref@example.org"
        self._register("Sonam", email)
        resp = accounts_route.my_space(self._authed(email, "/me"))
        body = self._body(resp)
        self.assertIn("Create Your Twin", body)
        self.assertIn("Teach Your Twin", body)
        for gone in ("Talk", ">Test<", "Activate", "Control", "/voice"):
            self.assertNotIn(gone, body)
        self._make_twin(email, "Son")
        resp = accounts_route.my_space(self._authed(email, "/me"))
        body = self._body(resp)
        self.assertIn("Update Your Twin", body)
        self.assertNotIn("Create Your Twin", body)


class TestTwinSkillsSourceOfTruth(RefineCase):
    def test_normalize_skill_list_dedupes(self):
        self.assertEqual(
            normalize_skill_list([" React ", "react", "REACT  ", "Python",
                                  "", "  ", 42, "Python"]),
            ["React", "Python"])

    def test_add_twin_skills_preserves_and_merges(self):
        email = " sami.ref@example.org".strip()
        self._register("Sami", email)
        tid = self._make_twin(email, "Sam", skills="Python")
        conn = self._conn()
        try:
            stored = add_twin_skills(conn, tid, ["python", " SQL "])
            conn.commit()
            self.assertEqual(stored, ["Python", "SQL"])
            again = add_twin_skills(conn, tid, ["sql", "Go"])
            conn.commit()
            self.assertEqual(again, ["Python", "SQL", "Go"])
        finally:
            conn.close()

    def test_find_skills_route_persists(self):
        email = "lena.ref@example.org"
        self._register("Lena", email)
        self._make_twin(email, "Len", skills="Python")
        resp = twin_route.find_add_skills(
            self._authed(email, "/find/skills", "POST"),
            skills="python, Rust", csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            from app.db import get_twin, twin_profile

            twin = get_twin_by_user(conn, self.accts[email]["uid"])
            self.assertEqual(twin_profile(twin)["skills"],
                             ["Python", "Rust"])
        finally:
            conn.close()

    def test_find_page_shows_skills_or_empty_message(self):
        email = "nina.ref@example.org"
        self._register("Nina", email)
        self._make_twin(email, "Nin")
        resp = twin_route.find_page(self._authed(email, "/find"))
        body = self._body(resp)
        self.assertIn("You haven't shared your skills with me yet.", body)
        self.assertIn('action="/find/skills"', body)
        self.assertIn(">Jobs<", body)
        self.assertIn(">Taxi<", body)
        self.assertIn(">Others<", body)

    def test_chat_skill_statement_persists_without_search(self):
        email = "oma.ref@example.org"
        self._register("Oma", email)
        tid = self._make_twin(email, "Oma")
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=self.accts[email]["uid"], twin_id=tid,
                text="Add Python, SQL to my skills.", twin_name="Oma")
            self.assertIsNone(out["need_id"])
            self.assertIn("Python", out["replies"][0])
            from app.db import get_twin, twin_profile

            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"],
                             ["Python", "SQL"])
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM requests"
                " WHERE owner_user_id = ?",
                (self.accts[email]["uid"],)).fetchone()["n"]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_job_search_runs_on_twin_skills(self):
        email = "pam.ref@example.org"
        self._register("Pam", email)
        tid = self._make_twin(email, "Pam", skills="Python, JavaScript",
                              location="Thimphu")
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=self.accts[email]["uid"], twin_id=tid,
                text="Find frontend jobs that match me.",
                twin_name="Pam")
            self.assertIsNotNone(out["need_id"])
            self.assertIn("Searching with your skills", out["replies"][0])
            req = need_engine.get_request(conn, out["need_id"])
            skills = need_engine.request_requirements(req)["skills"]
            self.assertIn("Python", skills)
            self.assertIn("JavaScript", skills)
        finally:
            conn.close()

    def test_skill_less_job_search_asks_then_saves(self):
        email = "quinn.ref@example.org"
        self._register("Quinn", email)
        tid = self._make_twin(email, "Qui")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=uid, twin_id=tid,
                text="Find jobs that match my skills.", twin_name="Qui")
            self.assertIsNotNone(out["need_id"])
            self.assertTrue(any("haven't shared your skills" in r
                                for r in out["replies"]))
            second = findchat.post_message(
                conn, user_id=uid, twin_id=tid, text="Python, SQL",
                twin_name="Qui")
            self.assertEqual(second["need_id"], out["need_id"])
            from app.db import get_twin, twin_profile

            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"],
                             ["Python", "SQL"])
            req = need_engine.get_request(conn, out["need_id"])
            self.assertIn("Python",
                          need_engine.request_requirements(req)["skills"])
        finally:
            conn.close()

    def test_search_text_skills_are_not_saved_to_twin(self):
        email = "ruth.ref@example.org"
        self._register("Ruth", email)
        tid = self._make_twin(email, "Rut", skills="Carpentry")
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=self.accts[email]["uid"], twin_id=tid,
                text="Find a Python developer for advice.",
                twin_name="Rut")
            req = need_engine.get_request(conn, out["need_id"])
            self.assertIn("Python",
                          need_engine.request_requirements(req)["skills"])
            from app.db import get_twin, twin_profile

            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"],
                             ["Carpentry"])
        finally:
            conn.close()

    def test_employer_requirements_never_touch_twin(self):
        email = "boss.ref@example.org"
        self._register("Boss", email)
        tid = self._make_twin(email, "Bos", skills="Management")
        conn = self._conn()
        try:
            opp_id = posting.publish_offer(
                conn, owner_user_id=self.accts[email]["uid"],
                title="Frontend dev wanted", description="React role.",
                skills=["React", "TypeScript"], location="Thimphu",
                category="job")
            self.assertTrue(opp_id)
            from app.db import get_twin, twin_profile

            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"],
                             ["Management"])
        finally:
            conn.close()


class TestNaturalLanguage(RefineCase):
    def test_job_category_exists(self):
        self.assertIn("job", CATEGORIES)

    def test_employee_posting_parses(self):
        # Raw parse stays generic; the post flow (classify + review)
        # upgrades employment phrasing to a job offer (see
        # test_post_review_marks_job_offers). Skills extract either way.
        out = parse_need("I need a frontend developer who knows React.")
        self.assertEqual(out["category"], "other")
        req = out["requirements"]
        self.assertIn("React", req["skills"])
        self.assertIn("frontend", [s.lower() for s in req["skills"]])

    def test_taxi_lowercase_places_parse(self):
        out = parse_need("Find me a taxi from thimphu to paro.")
        self.assertEqual(out["category"], "transport")
        self.assertEqual(out["requirements"]["origin"], "Thimphu")
        self.assertEqual(out["requirements"]["destination"], "Paro")

    def test_one_sided_taxi_question_bare_answer_fills(self):
        email = "fare.ref@example.org"
        self._register("Fare", email)
        tid = self._make_twin(email, "Far")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=uid, twin_id=tid,
                text="I need a taxi from Thimphu tomorrow.",
                twin_name="Far")
            self.assertTrue(any("Where are you headed" in r
                                for r in out["replies"]))
            second = findchat.post_message(
                conn, user_id=uid, twin_id=tid, text="Paro",
                twin_name="Far")
            self.assertEqual(second["need_id"], out["need_id"])
            req = need_engine.get_request(conn, out["need_id"])
            reqs = need_engine.request_requirements(req)
            self.assertEqual(reqs["origin"], "Thimphu")
            self.assertEqual(reqs["destination"], "Paro")
        finally:
            conn.close()

    def test_salary_range_parses(self):
        out = parse_need("Hiring a cook, salary 20000-30000.")
        self.assertEqual(out["requirements"]["salary_min"], 20000.0)
        self.assertEqual(out["requirements"]["salary_max"], 30000.0)
        single = parse_need("Developer role, pays Nu. 25000.")
        self.assertEqual(single["requirements"]["salary_min"], 25000.0)
        self.assertEqual(single["requirements"]["salary_max"], 25000.0)

    def test_service_request_parses(self):
        out = parse_need("I need someone who can repair an electrical"
                         " problem.")
        self.assertEqual(out["category"], "service")

    def test_classify_posting_employment_is_offer(self):
        self.assertEqual(classify_posting("I am looking for an employee."),
                         "offer")
        self.assertEqual(classify_posting(
            "I need a frontend developer who knows React."), "offer")
        self.assertEqual(classify_posting("Join my team as a designer."),
                         "offer")
        self.assertEqual(classify_posting(
            "I am looking for a taxi from Thimphu to Paro."), "need")
        self.assertEqual(classify_posting(
            "I need someone who can repair an electrical problem."),
                         "need")
        self.assertEqual(classify_posting("Find frontend jobs for me."),
                         "need")

    def test_post_review_marks_job_offers(self):
        review = posting.parse_posting(
            "I need a frontend developer who knows React.")
        self.assertEqual(review["kind"], "offer")
        self.assertEqual(review["category"], "job")
        self.assertIn("React", review["skills"])

    def test_intent_job_clarification(self):
        out = interpret_need("I want a job")
        self.assertEqual(out["category"], "job")

    def test_collaboration_is_peer_direction(self):
        self.assertEqual(
            matcher.need_direction(
                "Find people interested in collaborating on AI.",
                "collaboration"),
            "peer")
        self.assertEqual(
            matcher.need_direction("Looking for a cofounder for my app."),
            "peer")
        # Concrete categories keep supply/demand rules.
        self.assertEqual(
            matcher.need_direction("My partner and I need a taxi.",
                                   "transport"),
            "demand")
        self.assertEqual(
            matcher.need_direction("I am available to tutor Java."),
            "supply")


class TestMatchingRefinement(RefineCase):
    def test_job_transport_never_match(self):
        self.assertFalse(matcher.categories_compatible("job", "transport"))
        self.assertFalse(matcher.categories_compatible("transport", "job"))
        self.assertTrue(matcher.categories_compatible("job", "other"))
        self.assertTrue(matcher.categories_compatible("", "transport"))
        self.assertTrue(matcher.categories_compatible("job", "job"))
        verdict = matcher.compatible(
            {"skills": ["driving"]}, "taxi driver available",
            "supply",
            {"direction": "demand", "category": "job",
             "requirements": {}, "text": "need a driver",
             "attrs": {"tags": ["driving"]}, "skills": ["driving"]},
            search_category="transport")
        self.assertIsNone(verdict)

    def test_peer_collaboration_needs_match(self):
        from app.db import create_twin

        uid_a = self._register("Ann", "ann.ref@example.org")
        uid_b = self._register("Ben", "ben.ref@example.org")
        conn = self._conn()
        try:
            tid_a = create_twin(
                conn, user_id=uid_a, display_name="AnnTwin", persona="p",
                tone="t", avatar_seed="a", authorized_fields=[],
                profile={})
            tid_b = create_twin(
                conn, user_id=uid_b, display_name="BenTwin", persona="p",
                tone="t", avatar_seed="b", authorized_fields=[],
                profile={})
            conn.commit()
            import uuid as _uuid

            req_a, _ = need_engine.create_need(
                conn, twin_id=tid_a, owner_user_id=uid_a,
                category="collaboration",
                need_text="Looking for a cofounder for a school app.",
                requirements={}, idempotency_key=_uuid.uuid4().hex)
            need_engine.confirm_request(conn, req_a, uid_a)
            req_b, _ = need_engine.create_need(
                conn, twin_id=tid_b, owner_user_id=uid_b,
                category="collaboration",
                need_text="Cofounder wanted for a school app project.",
                requirements={}, idempotency_key=_uuid.uuid4().hex)
            need_engine.confirm_request(conn, req_b, uid_b)
            result = matcher.match_search(conn, req_a)
            self.assertEqual(result["created"], 1)
        finally:
            conn.close()

    def test_salary_disjoint_excluded(self):
        excluded = score_provider({"salary_min": 30000.0},
                                  {"salary_max": 25000.0})
        self.assertTrue(excluded.excluded_reason)
        self.assertEqual(excluded.score, 0)
        overlap = score_provider({"salary_min": 20000.0},
                                 {"salary_max": 25000.0})
        self.assertFalse(overlap.excluded_reason)
        self.assertGreater(overlap.score, 0)

    def test_expired_offers_never_surface(self):
        from app.db import create_opportunity

        seeker = "seeker.ref@example.org"
        owner = "owner.ref@example.org"
        self._register("Seeker", seeker)
        self._register("Owner", owner)
        self._make_twin(seeker, "See", skills="Python")
        self._make_twin(owner, "Own")
        conn = self._conn()
        try:
            seeker_twin = get_twin_by_user(
                conn, self.accts[seeker]["uid"])
            owner_uid = self.accts[owner]["uid"]
            past = "2001-01-01 00:00:00"
            create_opportunity(
                conn, owner_user_id=owner_uid, kind="job",
                title="Ancient Python role", description="Python work.",
                required_skills=["Python"], location="Thimphu",
                category="job", expires_at=past)
            fresh = create_opportunity(
                conn, owner_user_id=owner_uid, kind="job",
                title="Fresh Python role", description="Python work.",
                required_skills=["Python"], location="Thimphu",
                category="job")
            conn.commit()
            titles = [r["opportunity"]["title"]
                      for r in find_matches(conn, seeker_twin)]
            self.assertIn("Fresh Python role", titles)
            self.assertNotIn("Ancient Python role", titles)
            import uuid as _uuid

            req_id, _ = need_engine.create_need(
                conn, twin_id=seeker_twin["id"],
                owner_user_id=self.accts[seeker]["uid"], category="job",
                need_text="Find Python jobs for me.",
                requirements={"skills": ["Python"]},
                idempotency_key=_uuid.uuid4().hex)
            need_engine.confirm_request(
                conn, req_id, self.accts[seeker]["uid"])
            result = matcher.match_search(conn, req_id)
            self.assertEqual(result["created"], 1)
            from app.db import get_match_by_pair

            self.assertIsNotNone(get_match_by_pair(conn, req_id,
                                                   "opportunity", fresh))
        finally:
            conn.close()

    def test_offer_expiry_sweep_closes_and_notifies(self):
        from app.db import create_opportunity, get_opportunity

        owner = "exp.ref@example.org"
        self._register("Exp", owner)
        self._make_twin(owner, "Exp")
        conn = self._conn()
        try:
            opp_id = create_opportunity(
                conn, owner_user_id=self.accts[owner]["uid"],
                title="Short taxi offer", description="Ride.",
                category="transport",
                expires_at="2001-01-01 00:00:00")
            conn.commit()
            count = matcher.expire_due(conn, matcher.now_stamp())
            self.assertEqual(count, 1)
            self.assertEqual(get_opportunity(conn, opp_id)["status"],
                             "closed")
            notes = conn.execute(
                "SELECT title FROM notifications WHERE user_id = ?",
                (self.accts[owner]["uid"],)).fetchall()
            self.assertTrue(any("search period ended" in n["title"]
                                for n in notes))
        finally:
            conn.close()

    def _matched_pair(self):
        """One search + one matching offer; returns (conn-ish ids)."""
        from app.db import create_opportunity

        seeker = "m1.ref@example.org"
        poster = "m2.ref@example.org"
        self._register("MOne", seeker)
        self._register("MTwo", poster)
        self._make_twin(seeker, "Moa", skills="Python",
                        phone="111", email_addr="moa@private.example.org")
        self._make_twin(poster, "Mob", phone="222",
                        email_addr="mob@private.example.org")
        conn = self._conn()
        try:
            seeker_twin = get_twin_by_user(
                conn, self.accts[seeker]["uid"])
            opp_id = create_opportunity(
                conn, owner_user_id=self.accts[poster]["uid"], kind="job",
                title="Python role", description="Python work in Thimphu.",
                required_skills=["Python"], location="Thimphu",
                category="job")
            conn.commit()
            out = findchat.post_message(
                conn, user_id=self.accts[seeker]["uid"],
                twin_id=seeker_twin["id"],
                text="Find Python jobs for me.", twin_name="Moa")
            matches = conn.execute(
                "SELECT id FROM matches WHERE search_request_id = ?",
                (out["need_id"],)).fetchall()
            self.assertEqual(len(matches), 1)
            return seeker, poster, matches[0]["id"]
        finally:
            conn.close()

    def test_one_sided_accept_is_not_mutual(self):
        seeker, poster, match_id = self._matched_pair()
        conn = self._conn()
        try:
            from app.db import get_match

            matcher.accept_match(conn, match_id,
                                 self.accts[seeker]["uid"],
                                 message="Interview Monday 10am.")
            match = get_match(conn, match_id)
            self.assertEqual(match["status"], "accepted")
            self.assertFalse(matcher.is_mutual(match))
            self.assertEqual(match["accept_message"],
                             "Interview Monday 10am.")
            # No contact before mutual acceptance.
            self.assertEqual(matcher.peer_contact_for_match(
                conn, match, self.accts[seeker]["uid"]), {})
            self.assertEqual(matcher.peer_contact_for_match(
                conn, match, self.accts[poster]["uid"]), {})
        finally:
            conn.close()

    def test_mutual_accept_unlocks_contacts(self):
        seeker, poster, match_id = self._matched_pair()
        conn = self._conn()
        try:
            from app.db import get_match

            seeker_uid = self.accts[seeker]["uid"]
            poster_uid = self.accts[poster]["uid"]
            matcher.accept_match(conn, match_id, seeker_uid,
                                 message="Interview Monday 10am.")
            matcher.accept_match(conn, match_id, poster_uid)
            match = get_match(conn, match_id)
            self.assertTrue(matcher.is_mutual(match))
            seeker_view = matcher.peer_contact_for_match(
                conn, match, seeker_uid)
            self.assertEqual(seeker_view["phone"], "222")
            self.assertEqual(seeker_view["email"],
                             "mob@private.example.org")
            poster_view = matcher.peer_contact_for_match(
                conn, match, poster_uid)
            self.assertEqual(poster_view["phone"], "111")
            connected = conn.execute(
                "SELECT user_id, kind FROM notifications"
                " WHERE kind = 'connected'").fetchall()
            kinds = {(n["user_id"], n["kind"]) for n in connected}
            self.assertIn((seeker_uid, "connected"), kinds)
            self.assertIn((poster_uid, "connected"), kinds)
            # Repeat accepts never duplicate the connected notice.
            matcher.accept_match(conn, match_id, poster_uid)
            matcher.accept_match(conn, match_id, seeker_uid)
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications"
                " WHERE kind = 'connected'").fetchone()["n"]
            self.assertEqual(total, 2)
        finally:
            conn.close()

    def test_rejected_pair_never_rematches(self):
        seeker, poster, match_id = self._matched_pair()
        conn = self._conn()
        try:
            from app.db import get_match

            matcher.reject_match(conn, match_id,
                                 self.accts[poster]["uid"])
            self.assertEqual(get_match(conn, match_id)["status"],
                             "rejected")
            match = get_match(conn, match_id)
            first = matcher.match_search(conn, match["search_request_id"])
            self.assertEqual(first["created"], 0)
        finally:
            conn.close()


class TestPostAndFindPages(RefineCase):
    def test_post_suggestions_present(self):
        email = "uma.ref@example.org"
        self._register("Uma", email)
        resp = twin_route.post_page(self._authed(email, "/post"))
        body = self._body(resp)
        self.assertIn("I am looking for an employee.", body)
        self.assertIn("I am looking for a taxi.", body)
        self.assertIn("I am looking for something else.", body)

    def test_post_review_has_duration_and_salary(self):
        email = "vic.ref@example.org"
        self._register("Vic", email)
        self._make_twin(email, "Vic")
        resp = twin_route.post_submit(
            self._authed(email, "/post", "POST"),
            description="I am looking for an employee.",
            action="draft", csrf_token=self._csrf(email))
        body = self._body(resp)
        self.assertIn("Review before publishing", body)
        self.assertIn('name="duration"', body)
        self.assertIn('name="salary_min"', body)
        self.assertIn('name="salary_max"', body)
        self.assertIn(">Offer<", body)

    def test_offer_publish_confirms_duration(self):
        email = "wes.ref@example.org"
        self._register("Wes", email)
        self._make_twin(email, "Wes")
        desc = "I am looking for an employee."
        resp = twin_route.post_submit(
            self._authed(email, "/post", "POST"),
            description=desc, action="publish", title="Hire help",
            kind="offer", category="job", skills="Python",
            location="Thimphu", quantity="1", duration="1d",
            salary_min="20000", salary_max="30000",
            csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertIn("/opportunities/", location)
        self.assertIn("search%20duration%3A%201%20day", location)
        opp_id = int(location.split("/opportunities/")[1].split("?")[0])
        conn = self._conn()
        try:
            from app.db import get_opportunity

            opp = get_opportunity(conn, opp_id)
            self.assertEqual(opp["kind"], "job")
            self.assertIsNotNone(opp["expires_at"])
            import json as _json

            attrs = _json.loads(opp["attributes_json"])
            self.assertEqual(attrs["salary_min"], 20000.0)
            self.assertEqual(attrs["salary_max"], 30000.0)
        finally:
            conn.close()

    def test_need_publish_confirms_duration(self):
        email = "xio.ref@example.org"
        self._register("Xio", email)
        self._make_twin(email, "Xio")
        desc = "I need a taxi from Thimphu to Paro tomorrow."
        resp = twin_route.post_submit(
            self._authed(email, "/post", "POST"),
            description=desc, action="publish", title="Taxi ride",
            kind="need", category="transport", skills="",
            location="", quantity="1", duration="5h",
            salary_min="", salary_max="",
            csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertIn("/requests/", location)
        self.assertIn("search%20duration%3A%205%20hours", location)
        req_id = int(location.split("/requests/")[1].split("?")[0])
        conn = self._conn()
        try:
            req = need_engine.get_request(conn, req_id)
            self.assertEqual(req["status"], "open")
            self.assertIsNotNone(req["expires_at"])
            reqs = need_engine.request_requirements(req)
            self.assertEqual(reqs["origin"], "Thimphu")
            self.assertEqual(reqs["destination"], "Paro")
        finally:
            conn.close()

    def test_request_detail_results_before_edit(self):
        email = "yara.ref@example.org"
        self._register("Yara", email)
        tid = self._make_twin(email, "Yar", skills="Python")
        conn = self._conn()
        try:
            out = findchat.post_message(
                conn, user_id=self.accts[email]["uid"], twin_id=tid,
                text="Find Python jobs for me.", twin_name="Yar")
            req_id = out["need_id"]
        finally:
            conn.close()
        resp = requests_route.request_detail(
            self._authed(email, f"/requests/{req_id}"), req_id=req_id)
        body = self._body(resp)
        providers_at = body.index("Available providers")
        edit_at = body.index("Edit this post")
        self.assertLess(providers_at, edit_at)
        self.assertIn("<details>", body)

    def test_notification_read_goes_to_match(self):
        email = "zane.ref@example.org"
        self._register("Zane", email)
        self._make_twin(email, "Zan")
        conn = self._conn()
        try:
            from app.db import create_notification

            note_id = create_notification(
                conn, user_id=self.accts[email]["uid"], kind="match",
                title="New match", body="Someone matches.",
                link="/requests/7")
            create_notification(
                conn, user_id=self.accts[email]["uid"], kind="match",
                title="Another match", body="Someone else matches.",
                link="/requests/9")
            conn.commit()
        finally:
            conn.close()
        resp = requests_route.notification_read(
            self._authed(email, "/notifications/read", "POST"),
            notif_id=str(note_id), csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/requests/7")
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT is_read FROM notifications WHERE id = ?",
                (note_id,)).fetchone()
            self.assertEqual(row["is_read"], 1)
        finally:
            conn.close()
        page = requests_route.notification_list(
            self._authed(email, "/notifications"))
        self.assertIn(">Read<", self._body(page))

    def test_conversations_have_no_demo_section(self):
        email = "amy.ref@example.org"
        self._register("Amy", email)
        self._make_twin(email, "Amy")
        resp = chats_route.convo_list(self._authed(email, "/conversations"))
        body = self._body(resp)
        self.assertNotIn("Public demo chats", body)
        self.assertNotIn("No public demos yet", body)
        self.assertIn("My Conversation", body)


class TestTeachMemory(RefineCase):
    def test_manual_teach_approve_retrieval(self):
        from app.memories import relevant_memories

        email = "bea.ref@example.org"
        self._register("Bea", email)
        tid = self._make_twin(email, "Bea")
        resp = memories_route.memory_add(
            self._authed(email, "/memories/add", "POST"),
            content="Bea prefers morning standups.", category="preference",
            csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            pending = conn.execute(
                "SELECT id, status FROM memories WHERE twin_id = ?",
                (tid,)).fetchall()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["status"], "pending")
            mem_id = pending[0]["id"]
        finally:
            conn.close()
        resp = memories_route.memory_approve(
            self._authed(email, f"/memories/{mem_id}/approve", "POST"),
            mem_id=mem_id, content="",
            csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            hits = relevant_memories(conn, tid, "morning standups")
            self.assertTrue(any("standups" in h for h in hits))
        finally:
            conn.close()


class TestMigrationV15(RefineCase):
    def test_v14_database_gains_new_columns(self):
        import sqlite3

        from app.db import SCHEMA_VERSION

        conn = self._conn()
        try:
            try:
                conn.execute("ALTER TABLE matches DROP COLUMN"
                             " accept_message")
                conn.execute("ALTER TABLE opportunities DROP COLUMN"
                             " expires_at")
            except sqlite3.OperationalError:
                self.skipTest("SQLite too old for DROP COLUMN")
            conn.execute("UPDATE schema_version SET version = 14"
                         " WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        init_db(self.db)
        conn = self._conn()
        try:
            cols = lambda t: {  # noqa: E731
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({t})")}
            self.assertIn("accept_message", cols("matches"))
            self.assertIn("expires_at", cols("opportunities"))
            version = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()["version"]
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            conn.close()


class TestVoiceRemoval(RefineCase):
    def test_no_voice_routes_or_modules(self):
        import importlib.util as _util

        from app.main import app

        def walk(routes):
            out = []
            for route in routes:
                orig = getattr(route, "original_router", None)
                if orig is not None and hasattr(orig, "routes"):
                    out.extend(walk(orig.routes))
                else:
                    out.append(getattr(route, "path", ""))
            return out

        paths = walk(app.routes)
        self.assertGreater(len(paths), 50)  # proves the walk is real
        self.assertNotIn("/voice", paths)
        self.assertNotIn("/voice/command", paths)
        self.assertFalse([p for p in paths if "voice" in p.lower()])
        self.assertIn("/find/skills", paths)
        self.assertIn("/memories/add", paths)
        self.assertIsNone(_util.find_spec("app.voice"))
        self.assertIsNone(_util.find_spec("app.routes.voice"))
        self.assertIsNotNone(_util.find_spec("app.twin_setup"))
        self.assertIsNotNone(_util.find_spec("app.twin_update_nl"))
        self.assertIn("/twin/setup", paths)
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parent.parent
        self.assertFalse((root / "app" / "templates" / "voice.html").exists())
        self.assertFalse((root / "app" / "voice").exists())

    def test_health_endpoint_ok(self):
        from app.routes.health import healthz

        out = healthz()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["app"], "MIRA")


class TestMutualJourneyHTTP(TwinChatCase):
    """Two users, real records: search, match, notify, accept, connect."""

    def _search_and_match(self, email):
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, email)
            req = conn.execute(
                "SELECT id FROM requests WHERE owner_user_id = ?",
                (user["id"],)).fetchone()
            rows = conn.execute(
                "SELECT id FROM matches WHERE search_request_id = ?",
                (req["id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            return req["id"], rows[0]["id"]
        finally:
            conn.close()

    def _set_contact(self, email, phone, email_addr):
        from app.db import twin_profile, update_twin

        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, email)
            twin = get_twin_by_user(conn, user["id"])
            profile = twin_profile(twin)
            profile["phone"] = phone
            profile["personal_email"] = email_addr
            import json as _json

            authorized = _json.loads(twin["authorized_fields_json"])
            update_twin(conn, twin["id"],
                        display_name=twin["display_name"],
                        persona=twin["persona"], tone=twin["tone"],
                        profile=profile,
                        authorized_fields=authorized)
            conn.commit()
        finally:
            conn.close()

    def test_full_mutual_acceptance_over_http(self):
        from tests.test_e2e_flows import _hidden

        seeker = "journey.seeker@example.org"
        poster = "journey.poster@example.org"
        boss = self._login_client("JP", poster)
        self._make_twin(boss, "Bos")
        self._publish(boss, "Bos")
        scout = self._login_client("JS", seeker)
        self._make_twin(scout, "Sco")
        self._publish(scout, "Sco", skills="Python", location="Thimphu")
        self._set_contact(seeker, "555-0101", "seeker@example.org")
        self._set_contact(poster, "555-0202", "poster@example.org")
        # Poster publishes the job offer (an employer post).
        loc = self._post(boss, "Hiring a Python developer in Thimphu.",
                         kind="offer", skills="Python", location="Thimphu")
        self.assertIn("/opportunities/", loc)
        # Seeker searches with twin skills; a match is created.
        self._find(scout, "Find Python jobs for me.")
        need_id, match_id = self._search_and_match(seeker)
        # Poster is notified and can read about the match.
        notes = boss.get("/notifications")
        self.assertEqual(notes.status_code, 200)
        self.assertIn("has interest", notes.text)
        self.assertIn(">Read<", notes.text)
        incoming = boss.get("/requests")
        self.assertIn("JS", incoming.text)
        # Contact is NOT exposed before mutual acceptance.
        self.assertNotIn("555-0101", incoming.text)
        self.assertNotIn("seeker@example.org", incoming.text)
        seeker_page = scout.get(f"/requests/{need_id}")
        self.assertIn("undecided", seeker_page.text)
        self.assertNotIn("mutually accepted", seeker_page.text)
        self.assertNotIn("555-0202", seeker_page.text)
        self.assertNotIn("poster@example.org", seeker_page.text)
        # Search owner accepts with a message.
        token = _hidden(seeker_page.text, "csrf_token")
        accepted = scout.post(
            f"/matches/{match_id}/accept",
            data={"message": "Interview Monday 10am.",
                  "csrf_token": token},
            follow_redirects=False)
        self.assertEqual(accepted.status_code, 303)
        one_sided = boss.get("/requests")
        self.assertIn("Interview Monday 10am.", one_sided.text)
        self.assertNotIn("mutually accepted", one_sided.text)
        self.assertNotIn("555-0101", one_sided.text)
        # Poster accepts on their side: mutual, contacts unlock.
        token = _hidden(one_sided.text, "csrf_token")
        both = boss.post(
            f"/matches/{match_id}/accept",
            data={"csrf_token": token}, follow_redirects=False)
        self.assertEqual(both.status_code, 303)
        final_seeker = scout.get(f"/requests/{need_id}")
        self.assertIn("mutually accepted", final_seeker.text)
        self.assertIn("555-0202", final_seeker.text)
        self.assertIn("poster@example.org", final_seeker.text)
        self.assertNotIn("555-0101", final_seeker.text)
        final_poster = boss.get("/requests")
        self.assertIn("mutually accepted", final_poster.text)
        self.assertIn("555-0101", final_poster.text)
        self.assertIn("seeker@example.org", final_poster.text)
        self.assertNotIn("555-0202", final_poster.text)
        # Both sides got the connected notification.
        self.assertIn("connected", scout.get("/notifications").text)
        self.assertIn("connected", boss.get("/notifications").text)

    def test_route_shorthand_and_destination_only_taxi_search_over_http(self):
        rider = self._login_client("Route", "route.rider@example.org")
        self._make_twin(rider, "Rou")
        self._publish(rider, "Rou")

        # A bare known-city route is a complete transport request.
        body = self._find(rider, "Thimphu to Paro.")
        self.assertNotIn("What kind of help do you need", body)

        # A destination-only travel request asks for the missing origin,
        # then merges the answer without replacing the destination.
        body = self._find(rider, "I am going to Paro.")
        self.assertIn("Where should the trip start?", body)
        from app.db import get_connection, get_user_by_email
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "route.rider@example.org")
            requests = conn.execute(
                "SELECT * FROM requests WHERE owner_user_id = ?"
                " ORDER BY id", (user["id"],)).fetchall()
            self.assertEqual(len(requests), 2)
            bare = requests[0]
            partial = requests[1]
            self.assertEqual(bare["category"], "transport")
            self.assertIn('"origin": "Thimphu"', bare["requirements_json"])
            self.assertIn('"destination": "Paro"', bare["requirements_json"])
            self.assertEqual(partial["category"], "transport")
            self.assertIn('"destination": "Paro"', partial["requirements_json"])
        finally:
            conn.close()

        body = self._find(rider, "Thimphu")
        self.assertIn("updated your search", body)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "route.rider@example.org")
            req = conn.execute(
                "SELECT * FROM requests WHERE owner_user_id = ?"
                " ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
            self.assertIn('"origin": "Thimphu"', req["requirements_json"])
            self.assertIn('"destination": "Paro"', req["requirements_json"])
        finally:
            conn.close()

    def test_taxi_post_and_search_over_http(self):
        seeker = "journey.rider@example.org"
        driver = "journey.driver@example.org"
        wheel = self._login_client("JD", driver)
        self._make_twin(wheel, "Whe")
        self._publish(wheel, "Whe")
        rider = self._login_client("JR", seeker)
        self._make_twin(rider, "Rid")
        self._publish(rider, "Rid", location="Thimphu")
        self._post(wheel, "Taxi available daily from Thimphu to Paro.",
                   kind="offer")
        self._find(
            rider, "I need a taxi from Thimphu to Paro tomorrow.")
        need_id, _ = self._search_and_match(seeker)
        page = rider.get(f"/requests/{need_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Matches for this search", page.text)
        self.assertIn("Available providers", page.text)


class TestTextTwinInteractions(RefineCase):
    def test_update_requires_confirmation_then_persists_and_scoped_undo(self):
        from app.db import get_twin, twin_profile, update_twin
        email = "nl.update@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL", skills="Python")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            first = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="Set my headline to AI developer", twin_name="NL")
            self.assertIn("confirm", first["replies"][0])
            self.assertEqual(twin_profile(get_twin(conn, tid))["headline"], "")
            saved = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="confirm", twin_name="NL")
            self.assertIn("Saved", saved["replies"][0])
            profile = twin_profile(get_twin(conn, tid))
            self.assertEqual(profile["headline"], "AI developer")
            profile["skills"].append("Rust")
            twin = get_twin(conn, tid)
            import json
            update_twin(conn, tid, display_name=twin["display_name"],
                persona=twin["persona"], tone=twin["tone"], profile=profile,
                authorized_fields=json.loads(twin["authorized_fields_json"]))
            conn.commit()
            undone = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="undo update", twin_name="NL")
            self.assertIn("Undid fields", undone["replies"][0])
            profile = twin_profile(get_twin(conn, tid))
            self.assertEqual(profile["headline"], "")
            self.assertEqual(profile["skills"], ["Python", "Rust"])
        finally:
            conn.close()

    def test_profile_write_and_undo_record_share_confirmation_transaction(self):
        from unittest.mock import patch
        from app.db import get_twin, twin_profile
        email = "nl.atomic@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="Set my headline to AI developer", twin_name="NL")
            with patch("app.findchat._say", side_effect=RuntimeError("simulated response failure")):
                with self.assertRaises(RuntimeError):
                    findchat.post_message(conn, user_id=uid, twin_id=tid,
                        text="confirm", twin_name="NL")
            conn.rollback()
            self.assertEqual(twin_profile(get_twin(conn, tid))["headline"], "")
            action = conn.execute("SELECT body FROM twin_chats WHERE role = 'system' AND kind = 'twin-action'").fetchone()
            import json
            self.assertEqual(json.loads(action["body"])["status"], "pending")
        finally:
            conn.close()

    def test_experience_years_update_is_bounded_and_confirmed(self):
        from app.db import get_twin, twin_profile
        email = "nl.years@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL")
        conn = self._conn()
        try:
            invalid = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Set my experience years to 999", twin_name="NL")
            self.assertIn("0 to 80", invalid["replies"][0])
            self.assertIsNone(twin_profile(get_twin(conn, tid))["experience_years"])
            findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Set my experience years to 4", twin_name="NL")
            findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="confirm", twin_name="NL")
            self.assertEqual(twin_profile(get_twin(conn, tid))["experience_years"], 4)
        finally:
            conn.close()

    def test_ambiguous_edit_does_not_save_and_requires_specific_field(self):
        from app.db import get_twin, twin_profile
        email = "nl.ambiguous@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL", skills="Python")
        conn = self._conn()
        try:
            out = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Update my twin", twin_name="NL")
            self.assertIn("could not identify", out["replies"][0])
            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"], ["Python"])
            # A job need is still a search and does not edit personal skills.
            out = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="I need a React developer", twin_name="NL")
            self.assertIsNotNone(out["need_id"])
            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"], ["Python"])
        finally:
            conn.close()

    def test_confirmation_rejects_stale_preview_without_overwriting_form_change(self):
        from app.db import get_twin, twin_profile, update_twin
        import json
        email = "nl.stale@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="Set my headline to AI developer", twin_name="NL")
            twin = get_twin(conn, tid)
            profile = twin_profile(twin)
            profile["headline"] = "Senior developer"
            update_twin(conn, tid, display_name=twin["display_name"],
                persona=twin["persona"], tone=twin["tone"], profile=profile,
                authorized_fields=json.loads(twin["authorized_fields_json"]))
            conn.commit()
            out = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="confirm", twin_name="NL")
            self.assertIn("changed after that preview", out["replies"][0])
            self.assertEqual(twin_profile(get_twin(conn, tid))["headline"], "Senior developer")
        finally:
            conn.close()

    def test_requirements_edit_stays_separate_from_personal_skills(self):
        from app.db import get_twin, twin_profile
        email = "nl.requirements@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL", skills="Python")
        conn = self._conn()
        try:
            out = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Set my requirements to React, leadership", twin_name="NL")
            self.assertIn("confirm", out["replies"][0])
            findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="yes", twin_name="NL")
            profile = twin_profile(get_twin(conn, tid))
            self.assertEqual(profile["requirements"], ["React", "leadership"])
            self.assertEqual(profile["skills"], ["Python"])
        finally:
            conn.close()

    def test_teach_text_uses_pending_review_and_duplicate_is_not_overwrite(self):
        email = "nl.teach@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL")
        conn = self._conn()
        try:
            first = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Teach my twin that I prefer morning meetings", twin_name="NL")
            self.assertIn("pending", first["replies"][0])
            row = conn.execute("SELECT content, status FROM memories WHERE twin_id = ?", (tid,)).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["content"], "I prefer morning meetings")
            second = findchat.post_message(conn, user_id=self.accts[email]["uid"],
                twin_id=tid, text="Teach my twin that I prefer morning meetings", twin_name="NL")
            self.assertIn("already", second["replies"][0])
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM memories WHERE twin_id = ?", (tid,)).fetchone()["n"], 1)
        finally:
            conn.close()

    def test_guided_setup_prefills_existing_form_without_creating_twin(self):
        from app import twin_setup
        email = "nl.setup@example.org"
        self._register("NL", email)
        state = twin_setup.new_setup()
        first_field = twin_setup.current_field(state)
        twin_setup.setup_turn(state, "pause")
        self.assertEqual(state["phase"], "paused")
        twin_setup.setup_turn(state, "resume")
        self.assertEqual(state["phase"], "collect")
        self.assertEqual(twin_setup.current_field(state), first_field)
        answers = ["Nova", "AI developer", "professional", "Python, SQL", "3", "Thimphu", "Weekdays", "Robotics", "Builds useful tools", "Friendly"]
        for answer in answers:
            result = twin_setup.setup_turn(state, answer)
        self.assertEqual(state["phase"], "review")
        profile = twin_setup.profile_from_answers(state["answers"])
        self.assertEqual(profile["skills"], ["Python", "SQL"])
        self.assertNotIn("requirements", profile)
        conn = self._conn()
        try:
            self.assertIsNone(get_twin_by_user(conn, self.accts[email]["uid"]))
        finally:
            conn.close()
        page = accounts_route.twin_setup_page(self._authed(email, "/twin/setup"))
        self.assertIn("Nothing is saved", self._body(page))
        import json
        csrf = self._csrf(email)
        route_state = twin_setup.new_setup()
        for answer in answers:
            page = accounts_route.twin_setup_turn(
                self._authed(email, "/twin/setup", "POST"),
                state=json.dumps(route_state), message=answer, csrf_token=csrf)
            route_state = json.loads(page.context["state"])
        self.assertIn("Continue to profile form", self._body(page))
        page = accounts_route.twin_setup_turn(
            self._authed(email, "/twin/setup", "POST"),
            state=json.dumps(route_state), message="Continue to profile form",
            csrf_token=csrf)
        body = self._body(page)
        self.assertIn('value="Nova"', body)
        self.assertIn('value="Python, SQL"', body)
        conn = self._conn()
        try:
            self.assertIsNone(get_twin_by_user(conn, self.accts[email]["uid"]))
        finally:
            conn.close()

    def test_clear_skills_previews_destructive_edit_and_confirms_to_empty(self):
        from app.db import get_twin, twin_profile
        email = "nl.clear@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL", skills="Python, SQL")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            out = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="Clear my skills", twin_name="NL")
            self.assertIn("Proposed profile edit", out["replies"][0])
            self.assertIn("(empty)", out["replies"][0])
            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"],
                             ["Python", "SQL"])
            findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="confirm", twin_name="NL")
            self.assertEqual(twin_profile(get_twin(conn, tid))["skills"], [])
        finally:
            conn.close()

    def test_undo_preserves_later_field_edit_and_restores_other_fields(self):
        from app.db import get_twin, twin_profile, update_twin
        import json
        email = "nl.undoconflict@example.org"
        self._register("NL", email)
        tid = self._make_twin(email, "NL")
        uid = self.accts[email]["uid"]
        conn = self._conn()
        try:
            findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="Set my headline to AI developer and set my bio to QA bio",
                twin_name="NL")
            findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="confirm", twin_name="NL")
            twin = get_twin(conn, tid)
            profile = twin_profile(twin)
            profile["headline"] = "Senior developer"
            update_twin(conn, tid, display_name=twin["display_name"],
                persona=twin["persona"], tone=twin["tone"], profile=profile,
                authorized_fields=json.loads(twin["authorized_fields_json"]))
            conn.commit()
            out = findchat.post_message(conn, user_id=uid, twin_id=tid,
                text="undo update", twin_name="NL")
            self.assertIn("Kept later edits to: headline", out["replies"][0])
            profile = twin_profile(get_twin(conn, tid))
            self.assertEqual(profile["headline"], "Senior developer")
            self.assertEqual(profile["bio"], "")
        finally:
            conn.close()

    def test_guided_setup_cancel_discards_draft_without_creating_twin(self):
        from app import twin_setup
        email = "nl.setupcancel@example.org"
        self._register("NL", email)
        state = twin_setup.new_setup()
        twin_setup.setup_turn(state, "Nova")
        result = twin_setup.setup_turn(state, "cancel")
        self.assertTrue(result["cancelled"])
        self.assertIn("Nothing was saved", result["reply"])
        conn = self._conn()
        try:
            self.assertIsNone(get_twin_by_user(conn, self.accts[email]["uid"]))
        finally:
            conn.close()
