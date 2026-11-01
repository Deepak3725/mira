"""Phase 5 maintained tests: visibility, demo seed, privacy gates."""

import tempfile
import unittest
from pathlib import Path

from app import conversations as engine
from app.conversations import ConversationError
from app.db import (
    create_twin,
    create_user,
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    update_twin,
    visible_twins,
)
from app.seed import seed_demo_conversation, seed_demo_data, seed_demo_opportunities


class VisibilityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t5.db")
        init_db(self.db)
        seed_demo_data(self.db)
        self.conn = get_connection(self.db)
        self.deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        self.pema = get_user_by_email(self.conn, "pema.demo@example.com")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestVisibility(VisibilityCase):
    def test_seeds_public_to_anonymous(self):
        rows = visible_twins(self.conn, None)
        self.assertEqual(len(rows), 4)

    def test_private_hidden_from_others_visible_to_owner(self):
        uid = create_user(self.conn, name="Zed", role="professional",
                          email="zed@x.test")
        tid = create_twin(self.conn, user_id=uid, display_name="Zed",
                          persona="p", tone="t", profile={}, authorized_fields=[],
                          avatar_seed="z", is_public=False)
        self.conn.commit()
        anon = [t["id"] for t in visible_twins(self.conn, None)]
        self.assertNotIn(tid, anon)
        other = [t["id"] for t in visible_twins(self.conn, self.deepak["id"])]
        self.assertNotIn(tid, other)
        own = [t["id"] for t in visible_twins(self.conn, uid)]
        self.assertIn(tid, own)
        update_twin(self.conn, tid, display_name="Zed", persona="p", tone="t",
                    profile={}, authorized_fields=[], is_public=True)
        self.conn.commit()
        self.assertIn(tid, [t["id"] for t in visible_twins(self.conn, None)])

    def test_private_twin_cannot_be_contacted(self):
        pema_twin = get_twin_by_user(self.conn, self.pema["id"])
        update_twin(self.conn, pema_twin["id"], display_name=pema_twin["display_name"],
                    persona="p", tone="t", profile={}, authorized_fields=[],
                    is_public=False)
        self.conn.commit()
        deepak_twin = get_twin_by_user(self.conn, self.deepak["id"])
        with self.assertRaises(ConversationError):
            engine.create_conversation(
                self.conn, twin_a_id=deepak_twin["id"], twin_b_id=pema_twin["id"],
                created_by_user_id=self.deepak["id"])

    def test_owner_can_chat_with_own_private_twin(self):
        uid = create_user(self.conn, name="Solo", role="professional",
                          email="solo@x.test")
        t1 = create_twin(self.conn, user_id=uid, display_name="S1", persona="p",
                         tone="t", profile={}, authorized_fields=[], avatar_seed="s1",
                         is_public=False)
        # One twin per user is enforced at the route level, not the DB level
        # for flexibility; engine only needs distinct twins here.
        deepak_twin = get_twin_by_user(self.conn, self.deepak["id"])
        cid = engine.create_conversation(
            self.conn, twin_a_id=t1, twin_b_id=deepak_twin["id"],
            created_by_user_id=uid)
        self.assertIsNotNone(engine.get_conversation(self.conn, cid))


class TestDemoSeed(VisibilityCase):
    def test_demo_conversation_seeded_and_idempotent(self):
        seed_demo_opportunities(self.db)
        first = seed_demo_conversation(self.db)
        self.assertEqual(first, {"created": 1, "skipped": 0})
        second = seed_demo_conversation(self.db)
        self.assertEqual(second, {"created": 0, "skipped": 1})
        demos = engine.list_demos(self.conn)
        self.assertEqual(len(demos), 1)
        conv = engine.get_conversation(self.conn, demos[0]["id"])
        self.assertEqual(conv["purpose"], "recruitment")
        self.assertEqual(conv["name_a"], "Dee")
        self.assertEqual(engine.twin_message_count(self.conn, conv["id"]), 6)


class TestPublicOnlyMatching(VisibilityCase):
    def _private_twin(self):
        uid = create_user(self.conn, name="Priya", role="job_seeker",
                          email="priya@x.test")
        tid = create_twin(
            self.conn, user_id=uid, display_name="Pri", persona="p", tone="t",
            profile={"skills": ["Python", "GraphQL"], "experience_years": 5,
                     "location": "Secretville"},
            authorized_fields=["skills"], avatar_seed="priya")
        self.conn.commit()
        from app.db import get_twin

        return get_twin(self.conn, tid)

    def test_discovery_ignores_private_fields(self):
        from app import matching

        twin = self._private_twin()
        matches = matching.find_matches(self.conn, twin)
        blob = "\n".join(m["match"].explanation for m in matches)
        self.assertNotIn("Secretville", blob)
        self.assertNotIn("5 years", blob)

    def test_detail_match_ignores_private_fields(self):
        from app.matching import SeekerProfile
        from app.privacy import twin_public_view

        twin = self._private_twin()
        seeker = SeekerProfile.from_profile(twin_public_view(twin))
        self.assertIsNone(seeker.location)
        self.assertIsNone(seeker.experience_years)

    def test_recommendation_ignores_private_fields(self):
        from app.db import create_opportunity, update_twin

        twin = self._private_twin()
        update_twin(self.conn, twin["id"], display_name=twin["display_name"],
                    persona="p", tone="t",
                    profile={"skills": ["Python", "GraphQL"],
                             "experience_years": 5, "location": "Secretville"},
                    authorized_fields=[])
        self.conn.commit()
        opp_id = create_opportunity(
            self.conn, owner_user_id=self.deepak["id"], title="PyRole",
            required_skills=["Python", "GraphQL"], preferred_experience_years=2,
            location="Thimphu")
        deepak_twin = get_twin_by_user(self.conn, self.deepak["id"])
        cid = engine.create_conversation(
            self.conn, twin_a_id=twin["id"], twin_b_id=deepak_twin["id"],
            created_by_user_id=twin["user_id"], opportunity_id=opp_id,
            purpose="recruitment")
        text = engine.generate_recommendation(self.conn, cid)
        # Nothing authorized: zero coverage, zero private leakage.
        self.assertIn("0 of 2 required", text)
        self.assertNotIn("Secretville", text)


class TestInitiatorOwnership(VisibilityCase):
    def test_creator_must_own_initiating_twin(self):
        deepak_twin = get_twin_by_user(self.conn, self.deepak["id"])
        pema_twin = get_twin_by_user(self.conn, self.pema["id"])
        with self.assertRaises(ConversationError):
            engine.create_conversation(
                self.conn, twin_a_id=deepak_twin["id"], twin_b_id=pema_twin["id"],
                created_by_user_id=self.pema["id"])


if __name__ == "__main__":
    unittest.main()
