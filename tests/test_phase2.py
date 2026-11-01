"""Phase 2 maintained tests: matching engine, discovery, seeds (stdlib only)."""

import tempfile
import unittest
from pathlib import Path

from app import matching
from app.db import get_connection, get_twin_by_user, get_user_by_email, init_db
from app.matching import SeekerProfile, find_matches, score_opportunity
from app.seed import seed_demo_data, seed_demo_opportunities


def _seeker(**kwargs):
    base = {"skills": [], "experience_years": None, "location": None,
            "open_to_remote": False}
    base.update(kwargs)
    return SeekerProfile(
        skills=tuple(base["skills"]),
        experience_years=base["experience_years"],
        location=base["location"],
        open_to_remote=base["open_to_remote"],
    )


def _score(seeker, **kwargs):
    base = {"required_skills": [], "preferred_years": None,
            "location": None, "remote_ok": False}
    base.update(kwargs)
    return score_opportunity(seeker, **base)


class EngineTestCase(unittest.TestCase):
    def test_exact_skill_match(self):
        seeker = _seeker(skills=["Python", "SQL"])
        res = _score(seeker, required_skills=["python", "sql"])
        self.assertEqual(res.skill_points, 60.0)
        self.assertEqual(res.missing_skills, ())

    def test_partial_skill_match(self):
        seeker = _seeker(skills=["Python"])
        res = _score(seeker, required_skills=["Python", "Go", "Rust", "SQL"])
        self.assertEqual(res.skill_points, 15.0)
        self.assertEqual(res.matching_skills, ("Python",))
        self.assertEqual(res.missing_skills, ("Go", "Rust", "SQL"))

    def test_no_required_skills_is_neutral_full(self):
        res = _score(_seeker(), required_skills=[])
        self.assertEqual(res.skill_points, 60.0)
        self.assertIn("No specific skills", res.explanation)

    def test_experience_met_and_unset(self):
        self.assertEqual(_score(_seeker(experience_years=5), preferred_years=2).experience_points, 20.0)
        self.assertEqual(_score(_seeker(experience_years=2), preferred_years=2).experience_points, 20.0)
        self.assertEqual(_score(_seeker(), preferred_years=None).experience_points, 20.0)

    def test_experience_below_and_unknown(self):
        res = _score(_seeker(experience_years=1), preferred_years=4)
        self.assertEqual(res.experience_points, 5.0)
        self.assertIn("Below preferred", res.explanation)
        self.assertEqual(_score(_seeker(), preferred_years=4).experience_points, 0.0)

    def test_location_exact_and_case_insensitive(self):
        res = _score(_seeker(location="thimphu"), location="Thimphu")
        self.assertEqual(res.location_points, 20.0)
        self.assertIn("matches", res.explanation)

    def test_location_remote_bridge(self):
        seeker = _seeker(location="Paro", open_to_remote=True)
        res = _score(seeker, location="Thimphu", remote_ok=True)
        self.assertEqual(res.location_points, 20.0)

    def test_location_partial_and_mismatch(self):
        seeker = _seeker(location="Paro", open_to_remote=True)
        res = _score(seeker, location="Thimphu", remote_ok=False)
        self.assertEqual(res.location_points, 8.0)
        seeker2 = _seeker(location="Paro", open_to_remote=False)
        res2 = _score(seeker2, location="Thimphu", remote_ok=False)
        self.assertEqual(res2.location_points, 0.0)
        self.assertIn("differs", res2.explanation)

    def test_score_bounds_and_deterministic(self):
        seeker = _seeker(skills=["Python"], experience_years=3,
                         location="Thimphu", open_to_remote=True)
        kwargs = {"required_skills": ["Python", "Go"], "preferred_years": 2,
                  "location": "Thimphu", "remote_ok": True}
        first = _score(seeker, **kwargs)
        second = _score(seeker, **kwargs)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.score, 0)
        self.assertLessEqual(first.score, 100)
        # Perfect fit hits the ceiling exactly.
        perfect = _score(_seeker(skills=["A"], experience_years=9, location="X"),
                         required_skills=["a"], preferred_years=1, location="x")
        self.assertEqual(perfect.score, 100)


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t2.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        self.conn = get_connection(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _twin(self, email):
        user = get_user_by_email(self.conn, email)
        return get_twin_by_user(self.conn, user["id"])

    def test_deepak_discovers_karma_opportunity(self):
        twin = self._twin("deepak.demo@example.com")
        matches = find_matches(self.conn, twin)
        titles = [m["opportunity"]["title"] for m in matches]
        self.assertIn("Backend Developer", titles)
        top = next(m for m in matches if m["opportunity"]["title"] == "Backend Developer")
        self.assertEqual(top["owner"]["name"], "Karma")
        self.assertGreaterEqual(top["match"].score, 70)
        self.assertIn("GraphQL", top["match"].missing_skills)

    def test_closed_excluded(self):
        twin = self._twin("deepak.demo@example.com")
        titles = [m["opportunity"]["title"] for m in find_matches(self.conn, twin)]
        self.assertNotIn("API Integration Specialist", titles)
        titles_all = [
            m["opportunity"]["title"]
            for m in find_matches(self.conn, twin, status="all")
        ]
        self.assertIn("API Integration Specialist", titles_all)

    def test_own_opportunities_excluded_by_default(self):
        twin = self._twin("karma.demo@example.com")
        titles = [m["opportunity"]["title"] for m in find_matches(self.conn, twin)]
        self.assertNotIn("Backend Developer", titles)
        own = find_matches(self.conn, twin, include_own=True)
        self.assertIn("Backend Developer", [m["opportunity"]["title"] for m in own])

    def test_skills_filter(self):
        twin = self._twin("deepak.demo@example.com")
        matches = find_matches(self.conn, twin, skills_filter=["docker"])
        self.assertEqual([m["opportunity"]["title"] for m in matches], ["CI/CD Setup Gig"])

    def test_location_filter(self):
        twin = self._twin("deepak.demo@example.com")
        matches = find_matches(self.conn, twin, location_filter="remote")
        titles = sorted(m["opportunity"]["title"] for m in matches)
        self.assertEqual(titles, ["CI/CD Setup Gig", "Design System Collaboration"])
        matches = find_matches(self.conn, twin, location_filter="thimphu")
        self.assertEqual([m["opportunity"]["title"] for m in matches], ["Backend Developer"])

    def test_results_ranked_stably(self):
        twin = self._twin("deepak.demo@example.com")
        first = find_matches(self.conn, twin)
        second = find_matches(self.conn, twin)
        self.assertEqual(
            [(m["opportunity"]["id"], m["match"].score) for m in first],
            [(m["opportunity"]["id"], m["match"].score) for m in second],
        )
        scores = [m["match"].score for m in first]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_private_data_in_results(self):
        twin = self._twin("pema.demo@example.com")
        for item in find_matches(self.conn, twin):
            blob = repr(item)
            for secret in ("@example.com", "17-000", "Nu.", "private", "secret"):
                self.assertNotIn(secret, blob)

    def test_seeker_profile_parsing(self):
        profile = SeekerProfile.from_profile(
            {"skills": [" Python ", 42, ""], "experience_years": "lots",
             "location": " ", "open_to_remote": 1}
        )
        self.assertEqual(profile.skills, ("Python",))
        self.assertIsNone(profile.experience_years)
        self.assertIsNone(profile.location)
        self.assertTrue(profile.open_to_remote)

    def test_opportunity_seed_idempotent(self):
        result = seed_demo_opportunities(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 4)


if __name__ == "__main__":
    unittest.main()
