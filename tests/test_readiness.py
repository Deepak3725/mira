"""Production-readiness suite: ten fictional accounts exercising the real
product surface end to end (route handlers + engines on an isolated temp
database; no sockets, no network, mock provider only).

Covers registration/login/logout, twin setup, memories, publishing,
discovery, matching, twin chats, requests, decisions, pause/resume,
fallback scenarios, isolation, invalid input, sessions, persistence,
and provider safety. Temporary accounts live only in the temp DB.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app import config as config_mod
from app import conversations as engine
from app import db as db_mod
from app import privacy
from app import requests as need_engine
from app.db import (
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)
from app.matching import find_matches, find_providers
from app.memories import create_memory, list_memories
from app.routes import accounts as accounts_route
from app.routes import chats as chats_route
from app.routes import channels as channels_route
from app.routes import memories as memories_route
from app.routes import pages as pages_route
from app.routes import projects as projects_route
from app.routes import requests as requests_route
from app.seed import (
    seed_demo_conversation,
    seed_demo_data,
    seed_demo_drivers,
    seed_demo_memories,
    seed_demo_opportunities,
)

USERS = [
    # name, email, role, twin, persona, tone, skills, years, location
    ("Ava", "ava.t10@example.org", "job_seeker", "AvaTwin",
     "Backend developer seeking product roles.", "warm",
     "Python, JavaScript", "3", "Thimphu"),
    ("Ben", "ben.t10@example.org", "employer", "BenTwin",
     "Hiring lead at a fictional studio.", "direct",
     "", "", "Thimphu"),
    ("Cara", "cara.t10@example.org", "job_seeker", "CaraTwin",
     "Full-stack developer open to new roles.", "friendly",
     "JavaScript, Python", "3", "Thimphu"),
    ("Dan", "dan.t10@example.org", "professional", "DanTwin",
     "Product designer taking clients.", "creative",
     "Figma, Prototyping", "4", "Paro"),
    ("Eli", "eli.t10@example.org", "independent", "EliTwin",
     "Freelance DevOps engineer.", "pragmatic",
     "Docker, CI/CD", "5", "Remote"),
    ("Fay", "fay.t10@example.org", "employer", "FayTwin",
     "Data hiring manager.", "structured",
     "", "", "Thimphu"),
    ("Gus", "gus.t10@example.org", "job_seeker", "GusTwin",
     "Carpenter looking for local work.", "plain",
     "Carpentry, Woodwork", "6", "Punakha"),
    ("Han", "han.t10@example.org", "independent", "HanTwin",
     "Private consultant (unlisted twin).", "quiet",
     "Consulting", "8", "Remote"),
    ("Ivy", "ivy.t10@example.org", "job_seeker", "IvyTwin",
     "Data analyst seeking analytics roles.", "curious",
     "Python, SQL", "2", "Thimphu"),
    ("Jon", "jon.t10@example.org", "employer", "JonTwin",
     "Hiring for senior platform roles.", "formal",
     "", "", "London"),
]

AUTH_FIELDS = ["headline", "skills", "experience_years", "location",
               "availability"]


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


class ReadinessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ready.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        seed_demo_drivers(self.db)
        seed_demo_memories(self.db)
        seed_demo_conversation(self.db)
        auth.reset_rate_limits()
        self._saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db
        self._saved_settings = db_mod.settings
        db_mod.settings = config_mod.get_settings()
        self.accts = {}

    def tearDown(self):
        db_mod.settings = self._saved_settings
        if self._saved_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._saved_env
        self.tmp.cleanup()

    # -- flow helpers (real route handlers) ---------------------------
    def _conn(self):
        return get_connection(self.db)

    def _register(self, name, email, password="t10-Ready-99", role="professional"):
        resp = accounts_route.register(
            _request("/register", method="POST"), name=name, email=email,
            password=password, role=role, csrf_token=auth.csrf_token(None))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            uid = get_user_by_email(conn, email)["id"]
        finally:
            conn.close()
        return uid

    def _login(self, email, password="t10-Ready-99"):
        resp = accounts_route.login(
            _request("/login", method="POST"), email=email,
            password=password, csrf_token=auth.csrf_token(None))
        self.assertEqual(resp.status_code, 303)
        raw = resp.headers["set-cookie"].split(";")[0]
        _, _, token = raw.partition("=")
        return f"{auth.SESSION_COOKIE}={token}"

    def _authed(self, email, path="/me", method="GET"):
        return _request(path, self.accts[email]["cookie"], method)

    def _csrf(self, email):
        return auth.csrf_for_request(self._authed(email),
                                     self.accts[email]["uid"])

    def _make_twin(self, email, public=True):
        acct = self.accts[email]
        resp = accounts_route.twin_create(
            self._authed(email, "/twin/new", "POST"),
            display_name=acct["twin"], persona=acct["persona"], tone=acct["tone"],
            headline=f"{acct['name']} headline", skills=acct["skills"],
            experience_years=acct["years"], location=acct["location"],
            availability="Open", portfolio_url="", bio=f"{acct['name']} bio",
            company="", role_hiring="", requirements="", phone="000",
            personal_email=f"{acct['name'].lower()}@private.example.org",
            open_to_remote="yes", authorized=list(AUTH_FIELDS),
            public_listing="yes" if public else None,
            cond_max_price="", cond_auto_answer="yes",
            cond_require_approval=None, cond_distance="",
            cond_availability="", csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            twin = get_twin_by_user(conn, acct["uid"])
        finally:
            conn.close()
        self.assertIsNotNone(twin)
        return twin["id"]

    def _setup_account(self, spec, public=True):
        name, email, role, twin, persona, tone, skills, years, loc = spec
        uid = self._register(name, email, role=role)
        cookie = self._login(email)
        self.accts[email] = {"uid": uid, "cookie": cookie, "name": name,
                             "twin": twin, "persona": persona, "tone": tone,
                             "skills": skills, "years": years, "location": loc}
        tid = self._make_twin(email, public=public)
        self.accts[email]["tid"] = tid
        return email

    def _setup_ten(self):
        for spec in USERS:
            self._setup_account(spec, public=(spec[0] != "Han"))

    def _post_job(self, email, title, skills, years, location,
                  kind="job", remote=""):
        resp = pages_route.opportunity_create(
            self._authed(email, "/opportunities/new", "POST"), kind=kind,
            title=title, description=f"Fictional posting: {title}.",
            skills=skills, years=years, location=location, remote_ok=remote,
            category="", csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        return int(resp.headers["location"].rsplit("/", 1)[-1])

    def _teach(self, email, fact):
        twin_id = self.accts[email]["tid"]
        conn = self._conn()
        try:
            mid = create_memory(conn, twin_id=twin_id, content=fact,
                                source="readiness", status="pending",
                                category="skill")
            conn.commit()
        finally:
            conn.close()
        return mid

class TestTenAccountJourneys(ReadinessCase):
    def test_all_ten_register_login_twin_memories_persist(self):
        self._setup_ten()
        for spec in USERS:
            email = spec[1]
            keep = self._teach(email, f"{spec[0]} fact one for approval.")
            drop = self._teach(email, f"{spec[0]} fact two for rejection.")
            resp = memories_route.memory_approve(
                self._authed(email, method="POST"), mem_id=keep,
                content="", csrf_token=self._csrf(email))
            self.assertEqual(resp.status_code, 303)
            resp = memories_route.memory_reject(
                self._authed(email, method="POST"), mem_id=drop,
                csrf_token=self._csrf(email))
            self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            for spec in USERS:
                uid = get_user_by_email(conn, spec[1])["id"]
                twin = get_twin_by_user(conn, uid)
                self.assertEqual(len(list_memories(conn, twin["id"],
                                                   "approved")), 1)
                self.assertEqual(len(list_memories(conn, twin["id"],
                                                   "rejected")), 1)
        finally:
            conn.close()
        # Logout, back in, data persists.
        for spec in USERS:
            email = spec[1]
            resp = accounts_route.logout(
                self._authed(email, "/logout", "POST"),
                csrf_token=self._csrf(email))
            self.assertEqual(resp.status_code, 303)
            cookie = self._login(email)
            self.accts[email]["cookie"] = cookie
            resp = accounts_route.my_space(self._authed(email, "/me"))
            self.assertEqual(resp.status_code, 200)
            self.assertIn(spec[3], resp.body.decode("utf-8"))

    def test_recruitment_accept_journey(self):
        self._setup_ten()
        opp = self._post_job("ben.t10@example.org", "Frontend Developer",
                             "JavaScript, CSS", "2", "Thimphu")
        # Ava discovers through the real route + engine.
        resp = pages_route.discover(
            self._authed("ava.t10@example.org", "/discover"),
            twin_id=self.accts["ava.t10@example.org"]["tid"],
            skills="", location="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Frontend Developer", resp.body.decode("utf-8"))
        conn = self._conn()
        try:
            ava = get_user_by_email(conn, "ava.t10@example.org")
            twin = get_twin_by_user(conn, ava["id"])
            match = next(m for m in find_matches(conn, twin)
                         if m["opportunity"]["id"] == opp)["match"]
            self.assertGreater(match.score, 0)
        finally:
            conn.close()
        # Contact → live conversation owned by both sides.
        me = self.accts["ava.t10@example.org"]
        ben = self.accts["ben.t10@example.org"]
        resp = chats_route.convo_create(
            self._authed("ava.t10@example.org", "/conversations/new",
                         "POST"),
            twin_a_id=me["tid"], twin_b_id=ben["tid"],
            opportunity_id=str(opp), purpose="recruitment", is_demo=None,
            csrf_token=self._csrf("ava.t10@example.org"))
        self.assertEqual(resp.status_code, 303)
        cid = int(resp.headers["location"].rsplit("/", 1)[-1])
        for _ in range(3):
            resp = chats_route.convo_step(
                self._authed("ava.t10@example.org", method="POST"),
                conv_id=cid, csrf_token=self._csrf("ava.t10@example.org"))
            self.assertEqual(resp.status_code, 303)
        # Hackathon compliance: the summary action appends no
        # applicant verdict to recruitment chats.
        resp = chats_route.convo_recommend(
            self._authed("ben.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ben.t10@example.org"))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            blob = "\n".join(
                m["body"] for m in engine.list_messages(conn, cid))
            for banned in ("RECOMMENDED", "PROMISING", "WEAK fit",
                           "Verdict", "Recruitment summary"):
                self.assertNotIn(banned, blob)
            conv = engine.get_conversation(conn, cid)
            self.assertEqual(conv["status"], "active")
            audit = list(conn.execute(
                "SELECT action FROM audit_events WHERE conv_id = ?",
                (cid,)))
            self.assertTrue(len(audit) >= 3)
        finally:
            conn.close()
        for email, side in (("ava.t10@example.org", None),
                            ("ben.t10@example.org", None)):
            resp = chats_route.convo_decision(
                self._authed(email, method="POST"), conv_id=cid,
                decision="interested", csrf_token=self._csrf(email))
            self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            conv = engine.get_conversation(conn, cid)
            self.assertEqual(conv["decision_a"], "interested")
            self.assertEqual(conv["decision_b"], "interested")
        finally:
            conn.close()

    def test_rejection_then_manual_fallback(self):
        self._setup_ten()
        ben_job = self._post_job("ben.t10@example.org", "Frontend Developer",
                                 "JavaScript, CSS", "2", "Thimphu")
        fay_job = self._post_job("fay.t10@example.org", "Data Analyst",
                                 "Python, SQL", "2", "Thimphu")
        cara = self.accts["cara.t10@example.org"]
        ben = self.accts["ben.t10@example.org"]
        resp = chats_route.convo_create(
            self._authed("cara.t10@example.org", "/conversations/new",
                         "POST"),
            twin_a_id=cara["tid"], twin_b_id=ben["tid"],
            opportunity_id=str(ben_job), purpose="recruitment",
            is_demo=None, csrf_token=self._csrf("cara.t10@example.org"))
        cid = int(resp.headers["location"].rsplit("/", 1)[-1])
        chats_route.convo_step(
            self._authed("cara.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("cara.t10@example.org"))
        for email in ("cara.t10@example.org", "ben.t10@example.org"):
            chats_route.convo_decision(
                self._authed(email, method="POST"), conv_id=cid,
                decision="not-interested", csrf_token=self._csrf(email))
        conn = self._conn()
        try:
            conv = engine.get_conversation(conn, cid)
            self.assertEqual(conv["decision_a"], "not-interested")
            self.assertEqual(conv["decision_b"], "not-interested")
            # No false acceptance, no duplicate conversation.
            rows = list(conn.execute(
                "SELECT * FROM conversations WHERE opportunity_id = ?"
                " AND is_demo = 0", (ben_job,)))
            self.assertEqual(len(rows), 1)
            # Manual fallback: a fresh search surfaces Fay's job instead.
            user = get_user_by_email(conn, "cara.t10@example.org")
            twin = get_twin_by_user(conn, user["id"])
            titles = [m["opportunity"]["id"] for m in find_matches(conn, twin)]
            self.assertIn(fay_job, titles)
        finally:
            conn.close()

    def test_similar_scores_and_no_match(self):
        self._setup_ten()
        opp = self._post_job("ben.t10@example.org", "Frontend Developer",
                             "JavaScript, CSS", "2", "Thimphu")
        conn = self._conn()
        try:
            scores = {}
            for email in ("ava.t10@example.org", "cara.t10@example.org",
                          "gus.t10@example.org"):
                user = get_user_by_email(conn, email)
                twin = get_twin_by_user(conn, user["id"])
                match = next(m for m in find_matches(conn, twin)
                             if m["opportunity"]["id"] == opp)["match"]
                scores[email] = match.score
            self.assertLess(abs(scores["ava.t10@example.org"]
                                - scores["cara.t10@example.org"]), 15)
            self.assertLess(scores["gus.t10@example.org"],
                            scores["ava.t10@example.org"])
            # Deterministic order across repeated searches.
            user = get_user_by_email(conn, "ava.t10@example.org")
            twin = get_twin_by_user(conn, user["id"])
            first = [m["opportunity"]["id"] for m in find_matches(conn, twin)]
            second = [m["opportunity"]["id"] for m in find_matches(conn, twin)]
            self.assertEqual(first, second)
        finally:
            conn.close()

    def test_no_verdict_tiers_anywhere(self):
        # Hackathon compliance: no RECOMMENDED/PROMISING/WEAK verdict
        # tiers exist, even for weak-fit coordination chats.
        self._setup_ten()
        opp = self._post_job("jon.t10@example.org", "Senior Platform Engineer",
                             "Python, Kubernetes, Go, System Design, AWS",
                             "5", "London")
        ava = self.accts["ava.t10@example.org"]
        jon = self.accts["jon.t10@example.org"]
        resp = chats_route.convo_create(
            self._authed("ava.t10@example.org", "/conversations/new",
                         "POST"),
            twin_a_id=ava["tid"], twin_b_id=jon["tid"],
            opportunity_id=str(opp), purpose="recruitment", is_demo=None,
            csrf_token=self._csrf("ava.t10@example.org"))
        cid = int(resp.headers["location"].rsplit("/", 1)[-1])
        chats_route.convo_step(
            self._authed("ava.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ava.t10@example.org"))
        chats_route.convo_recommend(
            self._authed("ava.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ava.t10@example.org"))
        conn = self._conn()
        try:
            blob = "\n".join(
                m["body"] for m in engine.list_messages(conn, cid))
            for banned in ("RECOMMENDED", "PROMISING", "WEAK fit",
                           "Verdict", "Recruitment summary"):
                self.assertNotIn(banned, blob)
        finally:
            conn.close()


class TestRequestFallbacks(ReadinessCase):
    def _taxi_request(self, email):
        resp = requests_route.request_create(
            self._authed(email, "/requests/new", "POST"),
            need_text="I need a taxi from GCIT to Thimphu tomorrow at 9 AM.",
            category="transport", idem_key=f"taxi-{email}",
            csrf_token=self._csrf(email))
        self.assertEqual(resp.status_code, 303)
        return int(resp.headers["location"].split("/")[2])

    def test_taxi_shortlist_orders_by_budget(self):
        self._setup_ten()
        rid = self._taxi_request("ava.t10@example.org")
        resp = requests_route.request_confirm(
            self._authed("ava.t10@example.org", method="POST"), req_id=rid,
            category="transport", budget="500", origin="GCIT",
            destination="Thimphu", when="tomorrow at 9 AM", skills="",
            csrf_token=self._csrf("ava.t10@example.org"))
        self.assertEqual(resp.status_code, 303)
        resp = requests_route.request_detail(
            self._authed("ava.t10@example.org", f"/requests/{rid}"), rid)
        body = resp.body.decode("utf-8")
        self.assertIn("City Taxi (Thimphu valley)", body)
        self.assertNotIn("Premium Taxi (long trips)", body)
        conn = self._conn()
        try:
            req = need_engine.get_request(conn, rid)
            titles = [i["offering"]["title"]
                      for i in find_providers(conn, req)]
            self.assertIn("City Taxi (Thimphu valley)", titles)
            self.assertNotIn("Premium Taxi (long trips)", titles)
        finally:
            conn.close()

    def test_duplicate_request_reuses_row(self):
        self._setup_ten()
        first = requests_route.request_create(
            self._authed("ivy.t10@example.org", "/requests/new", "POST"),
            need_text="Need a designer for a logo.", category="service",
            idem_key="dup-key-1",
            csrf_token=self._csrf("ivy.t10@example.org"))
        second = requests_route.request_create(
            self._authed("ivy.t10@example.org", "/requests/new", "POST"),
            need_text="Need a designer for a logo.", category="service",
            idem_key="dup-key-1",
            csrf_token=self._csrf("ivy.t10@example.org"))
        self.assertEqual(first.headers["location"],
                         second.headers["location"])
        conn = self._conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE owner_user_id = ?",
                (self.accts["ivy.t10@example.org"]["uid"],)).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_contact_idempotent_and_withdraw(self):
        self._setup_ten()
        rid = self._taxi_request("eli.t10@example.org")
        requests_route.request_confirm(
            self._authed("eli.t10@example.org", method="POST"), req_id=rid,
            category="transport", budget="500", origin="GCIT",
            destination="Thimphu", when="tomorrow", skills="",
            csrf_token=self._csrf("eli.t10@example.org"))
        conn = self._conn()
        try:
            dorji = get_user_by_email(conn, "dorji.demo@example.com")
            offering = conn.execute(
                "SELECT id FROM opportunities WHERE owner_user_id = ?"
                " AND title = ?", (dorji["id"],
                                   "City Taxi (Thimphu valley)",)).fetchone()["id"]
        finally:
            conn.close()
        first = requests_route.request_contact(
            self._authed("eli.t10@example.org", method="POST"), req_id=rid,
            offering_id=offering,
            csrf_token=self._csrf("eli.t10@example.org"))
        second = requests_route.request_contact(
            self._authed("eli.t10@example.org", method="POST"), req_id=rid,
            offering_id=offering,
            csrf_token=self._csrf("eli.t10@example.org"))
        self.assertEqual(first.headers["location"],
                         second.headers["location"])
        resp = requests_route.request_close(
            self._authed("eli.t10@example.org", method="POST"), req_id=rid,
            csrf_token=self._csrf("eli.t10@example.org"))
        self.assertEqual(resp.status_code, 303)
        conn = self._conn()
        try:
            req = need_engine.get_request(conn, rid)
            self.assertEqual(req["status"], "closed")
        finally:
            conn.close()

    def test_pause_resume_and_decision_change(self):
        self._setup_ten()
        opp = self._post_job("fay.t10@example.org", "Data Analyst",
                             "Python, SQL", "2", "Thimphu")
        ivy = self.accts["ivy.t10@example.org"]
        fay = self.accts["fay.t10@example.org"]
        resp = chats_route.convo_create(
            self._authed("ivy.t10@example.org", "/conversations/new",
                         "POST"),
            twin_a_id=ivy["tid"], twin_b_id=fay["tid"],
            opportunity_id=str(opp), purpose="recruitment", is_demo=None,
            csrf_token=self._csrf("ivy.t10@example.org"))
        cid = int(resp.headers["location"].rsplit("/", 1)[-1])
        chats_route.convo_pause(
            self._authed("ivy.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ivy.t10@example.org"))
        chats_route.convo_step(
            self._authed("ivy.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ivy.t10@example.org"))
        conn = self._conn()
        try:
            self.assertEqual(engine.get_conversation(conn, cid)["status"],
                             "paused")
            self.assertEqual(engine.twin_message_count(conn, cid), 0)
        finally:
            conn.close()
        chats_route.convo_resume(
            self._authed("fay.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("fay.t10@example.org"))
        chats_route.convo_step(
            self._authed("ivy.t10@example.org", method="POST"),
            conv_id=cid, csrf_token=self._csrf("ivy.t10@example.org"))
        for choice in ("interested", "not-interested"):
            chats_route.convo_decision(
                self._authed("ivy.t10@example.org", method="POST"),
                conv_id=cid, decision=choice,
                csrf_token=self._csrf("ivy.t10@example.org"))
        conn = self._conn()
        try:
            conv = engine.get_conversation(conn, cid)
            self.assertEqual(conv["status"], "active")
            self.assertEqual(conv["decision_a"], "not-interested")
            decisions = list(conn.execute(
                "SELECT detail FROM audit_events WHERE conv_id = ?"
                " AND action = 'decision'", (cid,)))
            self.assertEqual(len(decisions), 2)
        finally:
            conn.close()


class TestIsolationAndRobustness(ReadinessCase):
    def test_cross_account_records_are_404(self):
        self._setup_ten()
        mid = self._teach("ava.t10@example.org", "Ava private fact.")
        rid_resp = requests_route.request_create(
            self._authed("ava.t10@example.org", "/requests/new", "POST"),
            need_text="Ava needs a taxi from X to Y.", category="transport",
            idem_key="iso-1", csrf_token=self._csrf("ava.t10@example.org"))
        rid = int(rid_resp.headers["location"].split("/")[2])
        proj = projects_route.project_create(
            self._authed("ava.t10@example.org", "/projects/new", "POST"),
            title="Ava project", goal="Secret goal",
            csrf_token=self._csrf("ava.t10@example.org"))
        pid = int(proj.headers["location"].rsplit("/", 1)[-1])
        chan = channels_route.channel_connect(
            self._authed("ava.t10@example.org", "/channels/connect",
                         "POST"),
            channel="mock", label="Ava mock", token="",
            csrf_token=self._csrf("ava.t10@example.org"))
        aid = int(chan.headers["location"].rsplit("/", 1)[-1])
        ben_post = self._authed("ben.t10@example.org", method="POST")
        ben_get = self._authed("ben.t10@example.org")
        with self.assertRaises(HTTPException):
            memories_route.memory_approve(
                ben_post, mem_id=mid, content="",
                csrf_token=self._csrf("ben.t10@example.org"))
        with self.assertRaises(HTTPException):
            requests_route.request_detail(ben_get, rid)
        with self.assertRaises(HTTPException):
            requests_route.request_review(ben_get, rid)
        with self.assertRaises(HTTPException):
            projects_route.project_detail(ben_get, pid)
        with self.assertRaises(HTTPException):
            channels_route.channel_detail(ben_get, aid)
        # ...while the owner retains full access (drafts review first).
        resp = requests_route.request_detail(
            self._authed("ava.t10@example.org"), rid)
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/requests/{rid}/review", resp.headers["location"])
        self.assertEqual(projects_route.project_detail(
            self._authed("ava.t10@example.org"), pid).status_code, 200)
        self.assertEqual(channels_route.channel_detail(
            self._authed("ava.t10@example.org"), aid).status_code, 200)

    def test_private_fields_never_cross_twins(self):
        self._setup_ten()
        opp = self._post_job("ben.t10@example.org", "Frontend Developer",
                             "JavaScript, CSS", "2", "Thimphu")
        ava = self.accts["ava.t10@example.org"]
        ben = self.accts["ben.t10@example.org"]
        resp = chats_route.convo_create(
            self._authed("ava.t10@example.org", "/conversations/new",
                         "POST"),
            twin_a_id=ava["tid"], twin_b_id=ben["tid"],
            opportunity_id=str(opp), purpose="recruitment", is_demo=None,
            csrf_token=self._csrf("ava.t10@example.org"))
        cid = int(resp.headers["location"].rsplit("/", 1)[-1])
        for _ in range(4):
            chats_route.convo_step(
                self._authed("ben.t10@example.org", method="POST"),
                conv_id=cid, csrf_token=self._csrf("ben.t10@example.org"))
        conn = self._conn()
        try:
            bodies = " ".join(m["body"]
                              for m in engine.list_messages(conn, cid))
            self.assertNotIn("private.example.org", bodies)
            twin = get_twin_by_user(conn, ava["uid"])
            view = privacy.twin_public_view(twin)
            self.assertNotIn("phone", view)
            self.assertNotIn("personal_email", view)
        finally:
            conn.close()

    def test_dashboard_shows_only_own_twin(self):
        self._setup_ten()
        resp = pages_route.dashboard(self._authed("ava.t10@example.org",
                                                  "/dashboard"))
        body = resp.body.decode("utf-8")
        self.assertIn("AvaTwin", body)
        self.assertNotIn("HanTwin", body)
        self.assertNotIn("Public twins", body)

    def test_invalid_inputs_rejected_safely(self):
        self._setup_ten()
        bad_need = requests_route.request_create(
            self._authed("ava.t10@example.org", "/requests/new", "POST"),
            need_text="   ", category="transport", idem_key="bad-1",
            csrf_token=self._csrf("ava.t10@example.org"))
        self.assertEqual(bad_need.status_code, 400)
        bad_twin = accounts_route.twin_create(
            self._authed("gus.t10@example.org", "/twin/new", "POST"),
            display_name="   ", persona="", tone="", headline="",
            skills="", experience_years="", location="", availability="",
            portfolio_url="", bio="", company="", role_hiring="",
            requirements="", phone="", personal_email="",
            open_to_remote=None, authorized=[], public_listing=None,
            cond_max_price="", cond_auto_answer=None,
            cond_require_approval=None, cond_distance="",
            cond_availability="",
            csrf_token=self._csrf("gus.t10@example.org"))
        # Blank display name is rejected even though Gus has a twin.
        self.assertEqual(bad_twin.status_code, 400)
        dupe_twin = accounts_route.twin_create(
            self._authed("gus.t10@example.org", "/twin/new", "POST"),
            display_name="Second Twin", persona="", tone="", headline="",
            skills="", experience_years="", location="", availability="",
            portfolio_url="", bio="", company="", role_hiring="",
            requirements="", phone="", personal_email="",
            open_to_remote=None, authorized=[], public_listing=None,
            cond_max_price="", cond_auto_answer=None,
            cond_require_approval=None, cond_distance="",
            cond_availability="",
            csrf_token=self._csrf("gus.t10@example.org"))
        # Second twin refused: redirected to edit, nothing duplicated.
        self.assertEqual(dupe_twin.status_code, 303)
        self.assertTrue(
            dupe_twin.headers["location"].endswith("/twin/edit"))
        with self.assertRaises(HTTPException):
            accounts_route.register(
                _request("/register", method="POST"), name="X",
                email="x@example.org", password="t10-Ready-99",
                role="professional", csrf_token="forged-token")

    def test_expired_and_revoked_sessions_rejected(self):
        self._setup_ten()
        uid = self.accts["ava.t10@example.org"]["uid"]
        stale = f"{auth.SESSION_COOKIE}={auth.create_session(uid, ttl=-100)}"
        resp = accounts_route.my_space(_request("/me", stale))
        self.assertEqual(resp.status_code, 303)
        resp = accounts_route.logout(
            self._authed("ava.t10@example.org", "/logout", "POST"),
            csrf_token=self._csrf("ava.t10@example.org"))
        self.assertEqual(resp.status_code, 303)
        resp = accounts_route.my_space(
            self._authed("ava.t10@example.org", "/me"))
        self.assertEqual(resp.status_code, 303)

    def test_registration_rate_limit_trips(self):
        self._setup_ten()
        codes = []
        for i in range(11):
            resp = accounts_route.register(
                _request("/register", method="POST"),
                name=f"RL{i}", email=f"rl{i}.t10@example.org",
                password="t10-Ready-99", role="professional",
                csrf_token=auth.csrf_token(None))
            codes.append(resp.status_code)
        # Ten setup registrations already consumed the hourly IP budget.
        self.assertTrue(all(c == 400 for c in codes))

    def test_manual_teach_and_approve_flow(self):
        self._setup_ten()
        email = "eli.t10@example.org"
        first = memories_route.memory_add(
            self._authed(email, "/memories/add", "POST"),
            content="Eli prefers morning deploys.", category="preference",
            csrf_token=self._csrf(email))
        self.assertEqual(first.status_code, 303)
        conn = self._conn()
        try:
            twin = get_twin_by_user(conn, self.accts[email]["uid"])
            pending = list_memories(conn, twin["id"], "pending")
            mem_id = next(m["id"] for m in pending
                          if "morning deploys" in m["content"])
        finally:
            conn.close()
        second = memories_route.memory_approve(
            self._authed(email, f"/memories/{mem_id}/approve", "POST"),
            mem_id=mem_id, content="",
            csrf_token=self._csrf(email))
        self.assertEqual(second.status_code, 303)
        conn = self._conn()
        try:
            twin = get_twin_by_user(conn, self.accts[email]["uid"])
            approved = list_memories(conn, twin["id"], "approved")
            self.assertTrue(any("morning deploys" in m["content"]
                                for m in approved))
        finally:
            conn.close()

    def test_data_persists_across_reopen(self):
        self._setup_ten()
        first = get_connection(self.db)
        try:
            before = first.execute(
                "SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        finally:
            first.close()
        second = get_connection(self.db)
        try:
            after = second.execute(
                "SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            row = get_user_by_email(second, "ava.t10@example.org")
        finally:
            second.close()
        self.assertEqual(before, after)
        self.assertIsNotNone(row)

    def test_provider_is_mock_only(self):
        from app.providers import get_provider

        self.assertEqual(get_provider().name, "mock")
        self.assertEqual(get_provider("nonexistent-vendor").name, "mock")
        self.assertEqual(config_mod.settings.ai_provider, "mock")


if __name__ == "__main__":
    unittest.main()

