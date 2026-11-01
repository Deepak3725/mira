"""Phase 4 maintained tests: import pipeline, memories, retrieval."""

import json
import tempfile
import unittest
from pathlib import Path

from app import memories
from app.db import get_twin_by_user, get_user_by_email, init_db
from app.import_pipeline import MAX_CANDIDATES, parse_chatgpt_export
from app.memories import MemoryError
from app.seed import seed_demo_data


def _node(role, text):
    return {"message": {"author": {"role": role}, "content": {"parts": [text]}}}


CHATGPT_FIXTURE = [
    {"title": "API help",
     "mapping": {"1": _node("user", "I built REST APIs with FastAPI for three years."),
                 "2": _node("assistant", "Nice! Tell me more."),
                 "3": _node("system", "ignore me"),
                 "4": {"broken": True}}},
    {"title": "Weekend",
     "mapping": {"1": _node("user", "I love hiking near Paro on weekends.")}},
]


class ImportCase(unittest.TestCase):
    def test_chatgpt_user_messages_only(self):
        out = parse_chatgpt_export(json.dumps(CHATGPT_FIXTURE).encode())
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], ("I built REST APIs with FastAPI for three years.",
                                  "chatgpt:API help"))
        self.assertTrue(all("Nice!" not in c and "ignore" not in c for c, _ in out))

    def test_messages_fallback_shape(self):
        raw = json.dumps([{"title": "t", "messages": [
            {"role": "user", "content": "Hello twin."},
            {"role": "assistant", "content": "Hi."}]}]).encode()
        self.assertEqual(parse_chatgpt_export(raw), [("Hello twin.", "chatgpt:t")])

    def test_generic_shape(self):
        raw = json.dumps({"memories": [
            {"content": "I know Docker.", "source": "manual"},
            {"content": "  "}, "nope"]}).encode()
        self.assertEqual(parse_chatgpt_export(raw), [("I know Docker.", "manual")])

    def test_rejects_garbage(self):
        with self.assertRaises(MemoryError):
            parse_chatgpt_export(b"not json")
        with self.assertRaises(MemoryError):
            parse_chatgpt_export(json.dumps({"nope": 1}).encode())
        with self.assertRaises(MemoryError):
            parse_chatgpt_export(json.dumps(
                [{"title": "t", "mapping": {"1": _node("assistant", "hi")}}]).encode())
        with self.assertRaises(MemoryError):
            parse_chatgpt_export(b"x" * (6 * 1024 * 1024))

    def test_dedupes_and_caps(self):
        convo = {"title": "t", "mapping": {
            str(i): _node("user", "Same sentence here.") for i in range(5)}}
        out = parse_chatgpt_export(json.dumps([convo]).encode())
        self.assertEqual(len(out), 1)
        big = {"title": "t", "mapping": {
            str(i): _node("user", f"Unique fact number {i} about APIs.") for i in range(500)}}
        out = parse_chatgpt_export(json.dumps([big]).encode())
        self.assertEqual(len(out), MAX_CANDIDATES)


class MemoryCase(unittest.TestCase):
    def setUp(self):
        from app.db import get_connection

        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t4.db")
        init_db(self.db)
        seed_demo_data(self.db)
        self.conn = get_connection(self.db)
        deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        karma = get_user_by_email(self.conn, "karma.demo@example.com")
        self.twin_d = get_twin_by_user(self.conn, deepak["id"])["id"]
        self.twin_k = get_twin_by_user(self.conn, karma["id"])["id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestMemories(MemoryCase):
    def test_review_lifecycle(self):
        mid = memories.create_memory(self.conn, twin_id=self.twin_d,
                                     content="I know FastAPI.", source="chatgpt:t")
        self.assertEqual(memories.get_memory(self.conn, mid)["status"], "pending")
        memories.set_status(self.conn, mid, "approved", new_content="I know FastAPI well.")
        row = memories.get_memory(self.conn, mid)
        self.assertEqual((row["status"], row["content"]),
                         ("approved", "I know FastAPI well."))
        memories.set_status(self.conn, mid, "rejected")
        self.assertEqual(memories.get_memory(self.conn, mid)["status"], "rejected")
        memories.delete_memory(self.conn, mid)
        self.assertIsNone(memories.get_memory(self.conn, mid))

    def test_dedupe_and_validation(self):
        memories.create_memory(self.conn, twin_id=self.twin_d, content="Same fact.")
        dup = memories.create_memory(self.conn, twin_id=self.twin_d,
                                     content="  same FACT. ")
        self.assertIsNone(dup)
        with self.assertRaises(MemoryError):
            memories.create_memory(self.conn, twin_id=self.twin_d, content="  ")
        with self.assertRaises(MemoryError):
            memories.create_memory(self.conn, twin_id=self.twin_d, content="x" * 3000)
        with self.assertRaises(MemoryError):
            memories.set_status(self.conn, 1, "archived")

    def test_isolation(self):
        mid = memories.create_memory(self.conn, twin_id=self.twin_d, content="Mine.")
        self.assertEqual(memories.get_memory(self.conn, mid)["twin_id"], self.twin_d)
        self.assertEqual(memories.list_memories(self.conn, self.twin_k), [])

    def test_retrieval_ranking(self):
        memories.create_memory(self.conn, twin_id=self.twin_d,
                               content="I built REST APIs with FastAPI.", status="approved")
        memories.create_memory(self.conn, twin_id=self.twin_d,
                               content="I love hiking.", status="approved")
        memories.create_memory(self.conn, twin_id=self.twin_d,
                               content="FastAPI and hiking both rock.", status="pending")
        out = memories.relevant_memories(self.conn, self.twin_d, "FastAPI REST APIs hiking")
        self.assertEqual(len(out), 2)
        self.assertIn("REST APIs", out[0])
        self.assertIn("hiking", out[1])
        self.assertEqual(memories.relevant_memories(self.conn, self.twin_d, "zzz qqq"), [])
        self.assertEqual(memories.relevant_memories(self.conn, self.twin_k, "FastAPI"), [])


class TestMemoryInChat(MemoryCase):
    def test_approved_memory_surfaces_in_reply(self):
        from app import conversations as engine

        mid = memories.create_memory(
            self.conn, twin_id=self.twin_d,
            content="I built REST APIs with FastAPI for three years in Thimphu.",
            source="chatgpt:t", status="approved")
        self.assertIsNotNone(mid)
        from app.db import get_twin_by_user as _twin, get_user_by_email as _user

        deepak = _user(self.conn, "deepak.demo@example.com")
        karma = _user(self.conn, "karma.demo@example.com")
        cid = engine.create_conversation(
            self.conn, twin_a_id=_twin(self.conn, deepak["id"])["id"],
            twin_b_id=_twin(self.conn, karma["id"])["id"],
            created_by_user_id=deepak["id"])
        engine.step_conversation(self.conn, cid, deepak["id"])
        engine.owner_note(self.conn, cid, karma["id"],
                          "What REST API experience with FastAPI do you have?")
        mems = memories.relevant_memories(
            self.conn, _twin(self.conn, deepak["id"])["id"],
            "What REST API experience with FastAPI do you have?")
        self.assertEqual(len(mems), 1)
        result = engine.step_conversation(self.conn, cid, deepak["id"], memories=mems)
        self.assertIn("From my owner's notes", result["message"]["body"])


if __name__ == "__main__":
    unittest.main()
