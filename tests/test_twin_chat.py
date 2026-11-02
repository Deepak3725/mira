"""Find Something (twin chat) + Post Something through the HTTP stack."""

import re
import unittest

from tests.test_e2e_flows import E2ECase, _client, _hidden


def _chat_text(page):
    import html as _html

    bodies = re.findall(r'<p class="bubble-body">(.*?)</p>', page, re.S)
    return "\n".join(_html.unescape(b) for b in bodies)


class TwinChatCase(E2ECase):
    def _login_client(self, name, email):
        client = _client()
        self.addCleanup(client.close)
        return self._register(client, name, email)

    def _publish(self, client, display, skills="", location="",
                 interests=""):
        page = client.get("/twin/edit").text
        data = {
            "display_name": display, "persona": "helpful",
            "tone": "friendly", "skills": skills, "location": location,
            "authorized": ["skills", "location"],
            "public_listing": "on",
            "csrf_token": _hidden(page, "csrf_token")}
        if interests:
            data["interests"] = interests
            data["authorized"] = ["skills", "location", "interests"]
        resp = client.post("/twin/edit", data=data,
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _find(self, client, message):
        page = client.get("/find").text
        resp = client.post("/find", data={
            "message": message,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return client.get("/find").text

    def _post(self, client, description, kind=None, title=None,
              skills="", location="", quantity="1"):
        page = client.get("/post").text
        resp = client.post("/post", data={
            "description": description, "action": "draft",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        review = resp.text
        self.assertIn("Review before publishing", review)
        data = {
            "description": description, "action": "publish",
            "title": title or description[:60],
            "kind": kind or ("offer" if ">Offer<" in review else "need"),
            "category": "other", "skills": skills, "location": location,
            "quantity": quantity,
            "csrf_token": _hidden(review, "csrf_token")}
        resp = client.post("/post", data=data, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return resp.headers["location"]


class TestFindChat(TwinChatCase):
    def test_nav_labels_exact(self):
        client = self._login_client("Nav", "nav.t@example.org")
        self._make_twin(client, "NavTwin")
        body = client.get("/find").text
        for label in ("Post Something", "Find Something",
                      "My Conversation", "Notification", "Twins"):
            self.assertIn(f">{label}<", body)
        anon = _client()
        self.addCleanup(anon.close)
        landing = anon.get("/").text
        self.assertIn("Log in", landing)
        self.assertIn("Create account", landing)
        self.assertNotIn("Find Something", landing)

    def test_find_requires_login_and_twin(self):
        anon = _client()
        self.addCleanup(anon.close)
        resp = anon.get("/find", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/login"))
        client = self._login_client("NoTwin", "notwin.t@example.org")
        resp = client.get("/find", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/twin/new"))

    def test_empty_state_and_heading(self):
        client = self._login_client("Empty", "empty.t@example.org")
        self._make_twin(client, "Emp")
        body = client.get("/find").text
        self.assertIn("What are you looking for?", body)
        self.assertIn("No messages yet", body)
        self.assertIn('name="message"', body)

    def test_job_search_honest_empty_then_matches(self):
        from app.db import get_connection, get_user_by_email

        deepak = self._login_client("Deepak", "deepak.t@example.org")
        self._make_twin(deepak, "Dee")
        self._publish(deepak, "Dee", skills="Python, JavaScript",
                      location="Thimphu")
        body = self._find(
            deepak, "I'm looking for a full-stack developer job"
            " in Bhutan. Preferably something relevant to my skills.")
        chat = _chat_text(body)
        self.assertIn("searching for", chat)
        self.assertIn("Using your profile", chat)
        self.assertIn("Nothing suitable yet", chat)
        self.assertNotIn("when_text", body)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "deepak.t@example.org")
            reqs = conn.execute(
                "SELECT * FROM requests WHERE owner_user_id = ?",
                (user["id"],)).fetchall()
            self.assertEqual(len(reqs), 1)
            self.assertEqual(reqs[0]["status"], "open")
            self.assertIsNotNone(reqs[0]["expires_at"])
            self.assertNotIn("when_text", reqs[0]["requirements_json"])
            chats = conn.execute(
                "SELECT * FROM twin_chats WHERE user_id = ?"
                " ORDER BY id", (user["id"],)).fetchall()
            self.assertTrue(any(c["role"] == "user" for c in chats))
            self.assertTrue(any(c["role"] == "twin" for c in chats))
        finally:
            conn.close()
        # Refresh keeps everything (PRG, no resubmission).
        again = deepak.get("/find").text
        self.assertIn("Nothing suitable yet", _chat_text(again))

        karma = self._login_client("Karma", "karma.t@example.org")
        self._make_twin(karma, "Kay")
        self._publish(karma, "Kay")
        loc = self._post(
            karma, "I'm hiring a full-stack developer for my team."
            " I need someone with JavaScript, Node.js, backend"
            " development, and frontend development skills."
            " The role is based in Bhutan.",
            kind="offer", title="Full-Stack Developer",
            skills="JavaScript, Node.js", location="Bhutan")
        self.assertIn("/opportunities/", loc)
        body = deepak.get("/find").text
        self.assertIn("Full-Stack Developer", body)
        self.assertIn("New match", deepak.get("/notifications").text)
        self.assertIn("has interest", karma.get("/notifications").text)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "deepak.t@example.org")
            req = conn.execute(
                "SELECT id FROM requests WHERE owner_user_id = ?",
                (user["id"],)).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM matches"
                " WHERE search_request_id = ?", (req["id"],)).fetchone()["n"]
            self.assertEqual(total, 1)
        finally:
            conn.close()

    def test_smalltalk_creates_no_need(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Small", "small.t@example.org")
        self._make_twin(client, "Sma")
        body = self._find(client, "hi")
        self.assertIn("what you're looking for", _chat_text(body))
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "small.t@example.org")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE owner_user_id"
                " = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()

    def test_help_me_find_creates_a_need(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Help", "help.t@example.org")
        self._make_twin(client, "Hel")
        body = self._find(
            client, "Help me find someone who can repair my laptop.")
        self.assertIn("searching for", _chat_text(body))
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "help.t@example.org")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE owner_user_id"
                " = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_bare_help_answers_without_a_need(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Help2", "help2.t@example.org")
        self._make_twin(client, "Hl2")
        body = self._find(client, "help")
        self.assertIn("describe what you're looking for",
                      _chat_text(body))
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "help2.t@example.org")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE owner_user_id"
                " = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()

    def test_taxi_asks_for_missing_route_then_merges(self):
        tashi = self._login_client("Tashi", "tashi.t@example.org")
        self._make_twin(tashi, "Tas")
        self._publish(tashi, "Tas")
        body = self._find(tashi, "Find me a taxi.")
        self.assertIn("Where should the trip start and end",
                      _chat_text(body))
        body = self._find(tashi, "From Thimphu to Dagana.")
        chat = _chat_text(body)
        self.assertIn("updated your search", chat)
        from app.db import get_connection, get_user_by_email
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "tashi.t@example.org")
            reqs = conn.execute(
                "SELECT * FROM requests WHERE owner_user_id = ?",
                (user["id"],)).fetchall()
            self.assertEqual(len(reqs), 1)
            self.assertIn("Thimphu", reqs[0]["requirements_json"])
        finally:
            conn.close()

    def test_taxi_with_route_and_seats_needs_no_questions(self):
        tashi = self._login_client("Taxi", "taxi.t@example.org")
        self._make_twin(tashi, "Tax")
        self._publish(tashi, "Tax")
        body = self._find(
            tashi, "I need a taxi from Thimphu to Dagana tomorrow."
            " I need four seats.")
        chat = _chat_text(body)
        self.assertNotIn("Where should the trip start and end", chat)
        from app.db import get_connection, get_user_by_email
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "taxi.t@example.org")
            req = conn.execute(
                "SELECT * FROM requests WHERE owner_user_id = ?",
                (user["id"],)).fetchone()
            self.assertEqual(req["quantity_requested"], 4)
            self.assertNotIn("four seats", req["requirements_json"])
        finally:
            conn.close()
        detail = tashi.get(f"/requests/{req['id']}").text
        self.assertIn("Accepted 0 of 4", detail)

    def test_piano_pair_different_wording(self):
        seeker = self._login_client("Seeker", "seeker.t@example.org")
        self._make_twin(seeker, "See")
        self._publish(seeker, "See")
        teacher = self._login_client("Teacher", "teacher.t@example.org")
        self._make_twin(teacher, "Tea")
        self._publish(teacher, "Tea")
        self._post(
            teacher, "I can provide piano lessons on weekends for"
            " beginners.", kind="offer", title="Piano lessons",
            location="Thimphu")
        body = self._find(
            seeker, "I want to find a piano teacher who can teach"
            " beginners on weekends.")
        self.assertIn("Piano lessons", body)

    def test_status_reply_lists_searches(self):
        client = self._login_client("Stat", "stat.t@example.org")
        self._make_twin(client, "Sta")
        body = self._find(client, "show my searches")
        self.assertIn("no active searches", _chat_text(body))
        self._find(client, "I'm looking for a Java tutor.")
        body = self._find(client, "show my searches")
        chat = _chat_text(body)
        self.assertIn("Your searches (1)", chat)
        self.assertIn("accepted 0 of 1", chat)

    def test_chat_is_owner_isolated(self):
        first = self._login_client("First", "first.t@example.org")
        self._make_twin(first, "Fir")
        self._find(first, "I'm looking for a unicorn keeper.")
        second = self._login_client("Second", "second.t@example.org")
        self._make_twin(second, "Sec")
        body = second.get("/find").text
        self.assertIn("No messages yet", body)
        self.assertNotIn("unicorn", body)

    def test_empty_message_rejected_without_side_effects(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Blank", "blank.t@example.org")
        self._make_twin(client, "Bla")
        page = client.get("/find").text
        resp = client.post("/find", data={
            "message": "   ",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please type", resp.text)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "blank.t@example.org")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM twin_chats WHERE user_id"
                " = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()

    def test_interests_round_trip_through_twin_edit(self):
        import json as _json

        from app.db import get_connection, get_twin_by_user, get_user_by_email
        from app.privacy import twin_public_view

        client = self._login_client("Int", "int.t@example.org")
        self._make_twin(client, "IntTwin")
        self._publish(client, "IntTwin", skills="Python",
                      location="Thimphu", interests="APIs, mentoring")
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "int.t@example.org")
            twin = get_twin_by_user(conn, user["id"])
            profile = _json.loads(twin["profile_json"])
            self.assertEqual(profile["interests"], ["APIs", "mentoring"])
            self.assertEqual(twin_public_view(twin)["interests"],
                             ["APIs", "mentoring"])
        finally:
            conn.close()

    def test_find_post_requires_csrf(self):
        client = self._login_client("Csrf", "csrf.t@example.org")
        self._make_twin(client, "Csr")
        resp = client.post("/find", data={
            "message": "hi", "csrf_token": "bogus"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

    def test_find_double_submit_creates_one_search(self):
        from app.db import (
            get_connection,
            get_user_by_email,
            list_requests_for_user,
        )

        client = self._login_client("Dup", "dup.t@example.org")
        self._make_twin(client, "DupTwin")
        message = "I'm looking for a Java tutor."
        self._find(client, message)
        self._find(client, message)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "dup.t@example.org")
            rows = [r for r in list_requests_for_user(
                conn, user["id"]) if r["status"] != "draft"]
            self.assertEqual(len(rows), 1)
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM twin_chats WHERE user_id"
                " = ? AND role = 'user'", (user["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_duplicate_window_expires(self):
        from app import findchat
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Old", "old.t@example.org")
        self._make_twin(client, "OldTwin")
        self._find(client, "hi")
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "old.t@example.org")
            self.assertTrue(
                findchat.is_duplicate_submit(conn, user["id"], "hi"))
            self.assertFalse(findchat.is_duplicate_submit(
                conn, user["id"], "something else"))
            conn.execute(
                "UPDATE twin_chats SET created_at = '2020-01-01 00:00:00'"
                " WHERE user_id = ?", (user["id"],))
            conn.commit()
            self.assertFalse(
                findchat.is_duplicate_submit(conn, user["id"], "hi"))
        finally:
            conn.close()


class TestPostingClassify(unittest.TestCase):
    def test_tiers(self):
        from app.posting import classify_posting as classify

        self.assertEqual(
            classify("I'm hiring a full-stack developer. I need"
                     " someone with JavaScript."), "offer")
        self.assertEqual(
            classify("I can provide piano lessons on weekends."),
            "offer")
        self.assertEqual(
            classify("I have a taxi available from Thimphu to Dagana."),
            "offer")
        self.assertEqual(
            classify("I need someone available on weekends."), "need")
        self.assertEqual(
            classify("Find me a taxi from Thimphu to Dagana."), "need")
        self.assertEqual(
            classify("Room available near town."), "offer")
        self.assertEqual(
            classify("I need a car for rent this weekend."), "need")
        self.assertEqual(
            classify("Is anyone available to tutor Java?"), "need")
        self.assertEqual(
            classify("I have a car for rent this weekend."), "offer")


class TestPostSomething(TwinChatCase):
    def test_post_requires_login_and_twin(self):
        anon = _client()
        self.addCleanup(anon.close)
        resp = anon.get("/post", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        client = self._login_client("NoTwinP", "notwinp.t@example.org")
        resp = client.get("/post")
        self.assertEqual(resp.status_code, 200)
        page = client.get("/post").text
        resp = client.post("/post", data={
            "description": "hello", "action": "draft",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/twin/new"))

    def test_offer_review_and_publish_triggers_matching(self):
        owner = self._login_client("Owner", "owner.t@example.org")
        self._make_twin(owner, "Own")
        self._publish(owner, "Own")
        seeker = self._login_client("Seeker2", "seeker2.t@example.org")
        self._make_twin(seeker, "Sk2")
        self._publish(seeker, "Sk2")
        self._find(seeker, "I want to learn piano on weekends.")
        loc = self._post(owner, "I can provide piano lessons on"
                         " weekends for beginners.", kind="offer",
                         title="Weekend piano")
        self.assertIn("/opportunities/", loc)
        body = seeker.get("/find").text
        self.assertIn("Weekend piano", body)

    def test_need_publish_keeps_parsed_slots(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Slots", "slots.t@example.org")
        self._make_twin(client, "Slo")
        loc = self._post(
            client, "I need a taxi from Thimphu to Dagana tomorrow.",
            kind="need", title="Taxi ride", location="")
        self.assertIn("/requests/", loc)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "slots.t@example.org")
            req = conn.execute(
                "SELECT requirements_json FROM requests"
                " WHERE owner_user_id = ?", (user["id"],)).fetchone()
            self.assertIn("Thimphu", req["requirements_json"])
            self.assertIn("Dagana", req["requirements_json"])
            self.assertIn("tomorrow", req["requirements_json"])
        finally:
            conn.close()

    def test_need_review_and_publish(self):
        client = self._login_client("Poster", "poster.t@example.org")
        self._make_twin(client, "Pos")
        loc = self._post(client, "I need four passengers to share the"
                         " cost of a taxi from Thimphu to Dagana.",
                         kind="need", title="Taxi cost share",
                         location="Thimphu", quantity="4")
        self.assertIn("/requests/", loc)
        detail = client.get(loc).text
        self.assertIn("Accepted 0 of 4", detail)

    def test_double_publish_reuses_post(self):
        from app.db import get_connection, get_user_by_email

        client = self._login_client("Dbl", "dbl.t@example.org")
        self._make_twin(client, "Dbl")
        desc = ("I can provide piano lessons on weekends"
                " for beginners.")
        first = self._post(client, desc, kind="offer",
                           title="Weekend piano")
        second = self._post(client, desc, kind="offer",
                            title="Weekend piano")
        self.assertEqual(first, second)
        need = "I need a laptop under $800."
        third = self._post(client, need, kind="need", title="Laptop")
        fourth = self._post(client, need, kind="need", title="Laptop")
        self.assertEqual(third, fourth)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, "dbl.t@example.org")
            opps = conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
                " WHERE owner_user_id = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(opps, 1)
            reqs = conn.execute(
                "SELECT COUNT(*) AS n FROM requests"
                " WHERE owner_user_id = ?", (user["id"],)).fetchone()["n"]
            self.assertEqual(reqs, 1)
        finally:
            conn.close()

    def test_post_requires_csrf(self):
        client = self._login_client("CsrfP", "csrfp.t@example.org")
        self._make_twin(client, "Csp")
        resp = client.post("/post", data={
            "description": "hi", "action": "draft",
            "csrf_token": "bogus"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
