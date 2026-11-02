"""Twin setup / update / chat / parity flows (voice+text shared)."""

import json
import tempfile
import unittest
from pathlib import Path

from app import twin_setup, twin_update_nl
from app.db import (
    create_opportunity,
    create_twin,
    get_connection,
    get_twin,
    get_twin_by_user,
    init_db,
    insert_returning_id,
)
from app.voice.commands import parse_command
from app.voice.session import handle_transcript, reset_sessions


class FlowCase(unittest.TestCase):
    def setUp(self):
        reset_sessions()
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "flows.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.owner = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email)"
            " VALUES (?, ?, ?)", ("Flo", "member", "flo@example.org"))
        self.other = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email)"
            " VALUES (?, ?, ?)", ("Oth", "member", "oth@example.org"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()
        reset_sessions()

    def _twin(self, uid=None, **kw):
        profile = kw.pop("profile", {"skills": ["Python"]})
        twin_id = create_twin(
            self.conn, user_id=self.owner if uid is None else uid,
            display_name=kw.pop("display_name", "Flo"),
            persona="p", tone="t", profile=profile,
            authorized_fields=kw.pop("authorized_fields", []),
            avatar_seed="s", **kw)
        self.conn.commit()
        return twin_id

    def _turn(self, text, user=None, twin="auto", provider="text"):
        if twin == "auto":
            row = get_twin_by_user(
                self.conn, self.owner if user is None else user)
            twin = row["id"] if row else None
        return handle_transcript(
            self.conn, user_id=self.owner if user is None else user,
            twin_id=twin, text=text, provider=provider)

    def _profile(self, twin_id):
        return json.loads(get_twin(self.conn, twin_id)[
            "profile_json"] or "{}")


class TestParserKinds(unittest.TestCase):
    def test_new_kinds(self):
        self.assertEqual(
            parse_command("MIRA, create my twin")["kind"], "setup")
        self.assertEqual(
            parse_command("update my twin and add Python")["kind"],
            "update")
        self.assertEqual(parse_command("undo")["kind"], "undo")
        self.assertEqual(
            parse_command("talk to my twin")["kind"], "chat")
        self.assertEqual(
            parse_command("teach my twin that I like tea")["kind"],
            "teach")
        self.assertEqual(
            parse_command("what do you remember?")["kind"],
            "remember")
        self.assertEqual(
            parse_command("approve memory 2")["kind"], "approve_mem")
        self.assertEqual(
            parse_command("reject memory 1")["kind"], "reject_mem")
        self.assertEqual(
            parse_command("find someone who can help with python")[
                "kind"], "find")
        self.assertEqual(
            parse_command("show opportunities")["kind"],
            "opportunities")
        self.assertEqual(
            parse_command("I need a plumber")["kind"], "need")
        self.assertEqual(
            parse_command("list my chats")["kind"], "my_chats")

    def test_setup_guard(self):
        # "make my twin remember X" is teaching, not setup.
        self.assertEqual(
            parse_command("make my twin remember this")["kind"],
            "teach")
        self.assertEqual(
            parse_command("tell my twin hi")["kind"], "message")

    def test_slots(self):
        teach = parse_command("teach my twin about my new project")
        self.assertEqual(teach["slots"]["fact"], "my new project")
        rem = parse_command("what do you remember about my education")
        self.assertEqual(rem["slots"]["topic"], "my education")
        num = parse_command("approve memory 2")
        self.assertEqual(num["slots"]["n"], 2)
        find = parse_command(
            "find someone who can help with web development")
        self.assertEqual(find["slots"]["query"], "web development")
        me_find = parse_command("Find me a frontend internship.")
        self.assertEqual(me_find["kind"], "find")
        self.assertEqual(me_find["slots"]["query"],
                         "frontend internship")
        me_someone = parse_command(
            "find me someone who can help with python")
        self.assertEqual(me_someone["slots"]["query"], "python")
        opp = parse_command("show opportunities for python")
        self.assertEqual(opp["slots"]["query"], "python")
        bare = parse_command("add Go to my skills")
        self.assertEqual(bare["kind"], "update")


class TestSetup(FlowCase):
    ANSWERS = ["Alex", "Fullstack developer", "BSc Computer Science",
               "5 years as a web developer", "Python, JavaScript",
               "cycling, chess", "async, no meetings",
               "friendly and brief", "lead a team",
               "I mentor juniors."]

    def _run_setup(self, providers=None):
        providers = providers or ["text"] * 11
        replies = [self._turn("create my twin",
                              provider=providers[0])]
        for answer, provider in zip(self.ANSWERS, providers[1:]):
            replies.append(self._turn(answer, provider=provider))
        return replies

    def test_full_voice_setup(self):
        replies = self._run_setup(["voice"] * 11)
        self.assertIn("approve", replies[-1]["text"].lower())
        self.assertIn("Alex", replies[-1]["text"])
        done = self._turn("approve", provider="voice")
        self.assertIn("active", done["text"].lower())
        twin = get_twin_by_user(self.conn, self.owner)
        self.assertIsNotNone(twin)
        self.assertFalse(twin["is_public"])
        self.assertEqual(json.loads(
            twin["authorized_fields_json"]), [])
        profile = self._profile(twin["id"])
        self.assertEqual(profile["skills"], ["Python", "JavaScript"])
        self.assertEqual(profile["experience_years"], 5)
        self.assertEqual(twin["tone"], "friendly and brief")
        audit = self.conn.execute(
            "SELECT action FROM audit_events WHERE actor_user_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (self.owner,)).fetchone()
        self.assertEqual(audit["action"], "twin.setup_completed")

    def test_switch_voice_text_mid_flow(self):
        replies = self._run_setup(
            ["voice", "text", "voice", "text", "voice", "text",
             "voice", "text", "voice", "text", "voice"])
        self.assertIn("approve", replies[-1]["text"].lower())
        done = self._turn("yes", provider="text")
        self.assertIn("active", done["text"].lower())
        self.assertIsNotNone(get_twin_by_user(self.conn,
                                              self.owner))

    def test_skip_back_change_pause_resume_cancel(self):
        self._turn("create my twin")
        self._turn("Alex")
        skipped = self._turn("skip")
        self.assertIn("education", skipped["text"].lower())
        back = self._turn("back")
        self.assertIn("headline", back["text"].lower())
        self._turn("Dev")
        changed = self._turn("change name to Alexander")
        self.assertIn("call your twin", changed["text"])
        self._turn("Alexander")
        paused = self._turn("pause")
        self.assertIn("resume", paused["text"].lower())
        resumed = self._turn("resume")
        self.assertIn("headline", resumed["text"].lower())
        # answer rest quickly then cancel at review
        for answer in ["H", "E", "X", "S", "I", "P", "C", "G"]:
            self._turn(answer)
        review = self._turn("B")
        self.assertIn("Alexander", review["text"])
        self.assertIn("headline: H", review["text"])
        cancelled = self._turn("cancel")
        self.assertIn("cancelled", cancelled["text"].lower())
        self.assertIsNone(get_twin_by_user(self.conn, self.owner))

    def test_edit_in_review(self):
        self._run_setup()
        edit = self._turn("edit skills")
        self.assertIn("skills", edit["text"].lower())
        review = self._turn("Rust")
        self.assertIn("Rust", review["text"])
        self._turn("approve")
        twin = get_twin_by_user(self.conn, self.owner)
        self.assertEqual(self._profile(twin["id"])["skills"],
                         ["Rust"])

    def test_command_shaped_answer_does_not_nuke_setup(self):
        self._turn("create my twin")
        self._turn("Alex")
        blocked = self._turn("build great products")
        self.assertIn("finish your twin", blocked["text"].lower())
        # Progress intact: still on the headline question.
        resumed = self._turn("A builder")
        self.assertIn("education", resumed["text"].lower())

    def test_second_setup_refused(self):
        self._twin()
        reply = self._turn("create my twin")
        self.assertIn("already have", reply["text"].lower())

    def test_users_isolated(self):
        self._turn("create my twin")
        self._turn("Alex")
        other = self._turn("create my twin", user=self.other)
        self.assertIn("call your twin", other["text"])
        mine = self._turn("Dev headline")
        self.assertIn("education", mine["text"].lower())


class TestUpdateParse(unittest.TestCase):
    def test_example_from_brief(self):
        parsed = twin_update_nl.parse_update(
            "update my twin. Remove Frontend Developer from my skills"
            " and add Fullstack Developer.")
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(
            [(o["op"], o["field"]) for o in parsed["ops"]],
            [("remove", "skills"), ("add", "skills")])
        self.assertEqual(parsed["ops"][0]["values"],
                         ["Frontend Developer"])
        self.assertEqual(parsed["ops"][1]["values"],
                         ["Fullstack Developer"])

    def test_set_replace_clear(self):
        parsed = twin_update_nl.parse_update(
            "set my headline to Senior Dev and replace Python with"
            " Rust in my skills and clear my interests")
        self.assertEqual(
            [(o["op"], o["field"]) for o in parsed["ops"]],
            [("set", "headline"), ("replace", "skills"),
             ("clear", "interests")])

    def test_my_field_is(self):
        parsed = twin_update_nl.parse_update("my location is Oslo")
        self.assertEqual(
            [(o["op"], o["field"], o["values"])
             for o in parsed["ops"]],
            [("set", "location", ["Oslo"])])

    def test_garbage_errors(self):
        parsed = twin_update_nl.parse_update("frobnicate the moon")
        self.assertEqual(parsed["ops"], [])
        self.assertTrue(parsed["errors"])
        bare = twin_update_nl.parse_update("add Python")
        self.assertEqual(bare["ops"], [])
        self.assertIn("which list", bare["errors"][0])


class TestUpdateApply(FlowCase):
    def test_apply_reports_before_after(self):
        self._twin()
        reply = self._turn(
            "update my twin and add Go to my skills")
        self.assertIn("skills", reply["text"])
        self.assertIn("Python", reply["text"])
        self.assertIn("Go", reply["text"])
        profile = self._profile(get_twin_by_user(
            self.conn, self.owner)["id"])
        self.assertEqual(profile["skills"], ["Python", "Go"])

    def test_brief_example_end_to_end(self):
        twin_id = self._twin(
            profile={"skills": ["Frontend Developer"]})
        reply = self._turn(
            "update my twin. Remove Frontend Developer from my"
            " skills and add Fullstack Developer.")
        self.assertIn("Fullstack Developer", reply["text"])
        profile = self._profile(twin_id)
        self.assertEqual(profile["skills"],
                         ["Fullstack Developer"])

    def test_dedupe_and_unrelated_preserved(self):
        twin_id = self._twin(profile={"skills": ["Python"],
                                      "bio": "hello"})
        reply = self._turn("add python to my skills")
        self.assertIn("already has", reply["text"].lower())
        profile = self._profile(twin_id)
        self.assertEqual(profile["skills"], ["Python"])
        self.assertEqual(profile["bio"], "hello")

    def test_remove_missing_reports(self):
        self._twin()
        reply = self._turn("remove Cobol from my skills")
        self.assertIn("has no Cobol", reply["text"])

    def test_destructive_needs_confirm(self):
        twin_id = self._twin()
        asked = self._turn("clear my skills")
        self.assertTrue(asked["expect_confirmation"])
        denied = self._turn("no")
        self.assertIn("No changes", denied["text"])
        self.assertEqual(self._profile(twin_id)["skills"],
                         ["Python"])
        self._turn("clear my skills")
        done = self._turn("yes")
        self.assertIn("empty", done["text"].lower())
        self.assertEqual(self._profile(twin_id)["skills"], [])

    def test_undo_restores(self):
        twin_id = self._twin()
        self._turn("set my headline to Senior Dev")
        self.assertEqual(self._profile(twin_id)["headline"],
                         "Senior Dev")
        undone = self._turn("undo")
        self.assertIn("Undone", undone["text"])
        self.assertEqual(self._profile(twin_id).get("headline", ""),
                         "")
        again = self._turn("undo")
        self.assertIn("Nothing to undo", again["text"])

    def test_bare_update_prompts(self):
        self._twin()
        reply = self._turn("update my twin")
        self.assertIn("What should I change", reply["text"])

    def test_trailing_period_does_not_break_fields(self):
        twin_id = self._twin()
        reply = self._turn("add Go to my skills.")
        self.assertIn("Go", reply["text"])
        self.assertIn("Go", self._profile(twin_id)["skills"])

    def test_bare_field_prompts_then_collects(self):
        twin_id = self._twin()
        asked = self._turn("Update my skills.")
        self.assertIn("What should I change about your skills",
                      asked["text"])
        done = self._turn("add Go")
        self.assertIn("Go", done["text"])
        self.assertIn("Go", self._profile(twin_id)["skills"])

    def test_collect_bare_text_adds_and_sets(self):
        twin_id = self._twin()
        self._turn("update my skills")
        done = self._turn("Go, Rust")
        self.assertIn("Go", done["text"])
        profile = self._profile(twin_id)
        self.assertIn("Go", profile["skills"])
        self.assertIn("Rust", profile["skills"])
        self._turn("update my headline")
        done = self._turn("Senior Dev")
        self.assertIn("Senior Dev", done["text"])
        self.assertEqual(self._profile(twin_id)["headline"],
                         "Senior Dev")

    def test_collect_cancel_and_nevermind(self):
        twin_id = self._twin()
        self._turn("update my skills")
        denied = self._turn("no")
        self.assertIn("Cancelled", denied["text"])
        self._turn("update my skills")
        nothing = self._turn("actually nothing")
        self.assertIn("Cancelled", nothing["text"])
        self.assertEqual(self._profile(twin_id)["skills"],
                         ["Python"])

    def test_collect_yes_does_not_crash(self):
        self._twin()
        self._turn("update my skills")
        reply = self._turn("yes")
        self.assertIn("Tell me the change", reply["text"])

    def test_collect_interrupted_by_command(self):
        self._twin()
        self._turn("update my skills")
        reply = self._turn("help")
        self.assertIn("Dropping the unconfirmed", reply["text"])
        self.assertIn("create my twin", reply["text"])

    def test_cross_user_refused(self):
        twin_id = self._twin()  # owner's twin
        with self.assertRaises(ValueError):
            twin_update_nl.apply(self.conn, self.other, twin_id,
                                 [{"op": "set", "field": "bio",
                                   "values": ["hijacked"],
                                   "target": ""}])
        self.assertNotIn("hijacked",
                         json.dumps(self._profile(twin_id)))

    def test_audited(self):
        self._twin()
        self._turn("add Go to my skills")
        rows = [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE actor_user_id = ?"
            " ORDER BY id", (self.owner,))]
        self.assertIn("twin.updated", rows)
        self._turn("undo")
        rows = [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE actor_user_id = ?"
            " ORDER BY id", (self.owner,))]
        self.assertIn("twin.update_undone", rows)


class TestTwinChat(FlowCase):
    def _seed_memory(self, twin_id, content, status="approved"):
        from app import memories

        mid = memories.create_memory(
            self.conn, twin_id=twin_id, content=content,
            source="import", status=status)
        self.conn.commit()
        return mid

    def test_enter_exit_and_guard(self):
        denied = self._turn("talk to my twin")
        self.assertIn("don't have a twin", denied["text"])
        self._twin()
        entered = self._turn("talk to my twin")
        self.assertIn("ready", entered["text"].lower())
        self.assertFalse(entered["done"])
        left = self._turn("stop")
        self.assertIn("Leaving our chat", left["text"])

    def test_ask_uses_approved_only(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "I prefer tea over coffee.")
        self._seed_memory(twin_id, "Secret launch plan.",
                           status="pending")
        self._turn("talk to my twin")
        reply = self._turn("do I like tea?")
        self.assertIn("tea over coffee", reply["text"])
        self.assertNotIn("launch plan", reply["text"])
        self.assertFalse(reply["done"])

    def test_follow_up_uses_topic(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "I prefer tea over coffee.")
        self._turn("talk to my twin", provider="voice")
        self._turn("do I like tea?", provider="voice")
        follow = self._turn("and what else?", provider="text")
        self.assertIn("Following our topic", follow["text"])
        self.assertIn("tea over coffee", follow["text"])

    def test_teach_and_approve_latest(self):
        twin_id = self._twin()
        self._turn("talk to my twin")
        taught = self._turn("teach my twin that I run marathons")
        self.assertIn("pending", taught["text"].lower())
        approved = self._turn("approve latest")
        self.assertIn("Approved", approved["text"])
        from app import memories

        rows = memories.list_memories(self.conn, twin_id,
                                      "approved")
        self.assertTrue(any("marathons" in r["content"]
                            for r in rows))

    def test_remember_lists_and_filters(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "I prefer tea over coffee.")
        self._seed_memory(twin_id, "BSc from Oslo University.")
        all_rem = self._turn("what do you remember?")
        self.assertIn("2 thing", all_rem["text"])
        topic = self._turn("what do you remember about Oslo?")
        self.assertIn("Oslo", topic["text"])
        self.assertNotIn("tea over coffee", topic["text"])

    def test_show_pending_and_reject(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "Draft fact.", status="pending")
        shown = self._turn("show pending")
        self.assertIn("Draft fact", shown["text"])
        removed = self._turn("reject memory 1")
        self.assertIn("Removed", removed["text"])
        from app import memories

        self.assertEqual(
            memories.list_memories(self.conn, twin_id,
                                   "pending"), [])

    def test_correction_flow(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "I prefer tea over coffee.")
        self._turn("talk to my twin")
        self._turn("do I like tea?")
        proposed = self._turn("no, actually I prefer green tea")
        self.assertTrue(proposed["expect_confirmation"])
        done = self._turn("yes")
        self.assertIn("correction is pending", done["text"])
        from app import memories

        old = memories.list_memories(self.conn, twin_id,
                                     "rejected")
        new = memories.list_memories(self.conn, twin_id, "pending")
        self.assertTrue(any("tea over coffee" in r["content"]
                            for r in old))
        self.assertTrue(any("green tea" in r["content"]
                            for r in new))

    def test_interruption_runs_command(self):
        twin_id = self._twin()
        self._turn("talk to my twin")
        reply = self._turn("build a treehouse planner")
        self.assertIn("Leaving our chat", reply["text"])
        self.assertIn("treehouse", reply["text"].lower())
        self.assertTrue(reply["expect_confirmation"])

    def test_chat_isolated_per_user(self):
        twin_id = self._twin()
        self._seed_memory(twin_id, "I prefer tea over coffee.")
        other_twin = self._twin(uid=self.other)
        self._turn("talk to my twin")
        self._turn("talk to my twin", user=self.other)
        reply = self._turn("do I like tea?", user=self.other,
                           twin=other_twin)
        self.assertIn("don't know", reply["text"])


class TestParity(FlowCase):
    def _opp(self, owner, title, skills):
        opp_id = create_opportunity(
            self.conn, owner_user_id=owner, kind="service",
            title=title, description="d",
            required_skills=skills)
        self.conn.commit()
        return opp_id

    def test_find_routes_to_matching(self):
        self._twin(authorized_fields=["skills"])
        self._twin(uid=self.other, display_name="Helper")
        self._opp(self.other, "Python mentoring", ["python"])
        reply = self._turn("find someone who can help with python")
        self.assertIn("Python mentoring", reply["text"])
        self.assertIn("score", reply["text"].lower())

    def test_find_empty(self):
        self._twin()
        reply = self._turn("find someone who can help with cobol")
        self.assertIn("No open opportunities", reply["text"])

    def test_opportunities_with_and_without_twin(self):
        self._twin(uid=self.other, display_name="Helper")
        self._opp(self.other, "Logo design", ["design"])
        mine = self._turn("show opportunities")
        self.assertIn("Logo design", mine["text"])
        self._twin()
        ranked = self._turn("show opportunities")
        self.assertIn("Logo design", ranked["text"])

    def test_need_posts_request(self):
        self._twin()
        asked = self._turn("I need a plumber tomorrow")
        self.assertIn("How long should I search", asked["text"])
        proposed = self._turn("2 hours")
        self.assertTrue(proposed["expect_confirmation"])
        done = self._turn("yes")
        self.assertIn("Posted request #", done["text"])
        row = self.conn.execute(
            "SELECT status, category, expires_at FROM requests").fetchone()
        self.assertEqual(row["status"], "open")
        self.assertTrue(row["expires_at"])

    def test_need_cancel(self):
        self._twin()
        self._turn("I need a plumber")
        denied = self._turn("no")
        self.assertIn("not posted", denied["text"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_my_chats(self):
        from app import conversations as engine

        empty = self._turn("list my chats")
        self.assertIn("no twin chats", empty["text"].lower())
        a = self._twin()
        b = self._twin(uid=self.other, display_name="Helper")
        twin_b = get_twin(self.conn, b)
        from app.db import update_twin

        update_twin(self.conn, b,
                    display_name=twin_b["display_name"],
                    persona="p", tone="t", profile={},
                    authorized_fields=[], is_public=True)
        self.conn.commit()
        engine.create_conversation(
            self.conn, twin_a_id=a, twin_b_id=b,
            created_by_user_id=self.owner)
        listed = self._turn("list my chats")
        self.assertIn("Helper", listed["text"])

    def test_guards_need_twin(self):
        for text in ("find someone for python",
                     "I need a plumber",
                     "teach my twin X",
                     "what do you remember?",
                     "update my twin and add X to my skills"):
            reply = self._turn(text)
            self.assertIn("twin", reply["text"].lower(),
                          text)

    def test_voice_page_quick_prompts(self):
        page = (Path(__file__).resolve().parent.parent
                / "app" / "templates" / "voice.html")
        text = page.read_text(encoding="utf-8")
        for prompt in ("create my twin", "talk to my twin",
                       "update my twin"):
            self.assertIn(f'data-say="{prompt}"', text)
