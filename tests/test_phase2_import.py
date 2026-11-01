"""Phase 2 tests: universal knowledge import (detect, parsers, zip,
streaming, categories, conflicts, Meta AI honesty, v9 migration)."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import memories
from app.db import SCHEMA_VERSION, driver_name, get_connection, init_db
from app.importers import (
    MAX_CANDIDATES,
    ChatGPTImporter,
    ClaudeImporter,
    GeminiImporter,
    GenericImporter,
    MetaAIImporter,
    classify,
    detect,
    find_conflicts,
    import_bytes,
    import_upload,
)
from app.importers.archive import extract_members
from app.importers.streaming import iter_json_array_items
from app.memories import MemoryError

HERE = Path(__file__).resolve().parent


def _mapping_user(text, create_time=None):
    message = {"author": {"role": "user"},
               "content": {"parts": [text]}}
    if create_time is not None:
        message["create_time"] = create_time
    return {"message": message}


class TestDetect(unittest.TestCase):
    def test_matrix(self):
        chatgpt = [{"title": "t", "mapping": {"a": {}}}]
        claude = [{"uuid": "u", "name": "n", "chat_messages": []}]
        gemini = [{"header": "Gemini Apps", "safeHtmlItem": [],
                   "title": "Prompted x"}]
        generic = {"memories": [{"content": "x"}]}
        self.assertIsInstance(detect(chatgpt, "conversations.json"),
                              ChatGPTImporter)
        self.assertIsInstance(detect(claude, "conversations.json"),
                              ClaudeImporter)
        self.assertIsInstance(detect(gemini, "MyActivity.json"),
                              GeminiImporter)
        self.assertIsInstance(detect(generic, "hand.json"),
                              GenericImporter)
        self.assertIsNone(detect([{"weird": 1}], "other.json"))
        self.assertIsNone(detect([], "empty.json"))

    def test_chatgpt_wins_over_claude_on_mapping(self):
        both = [{"title": "t", "mapping": {"a": {}},
                 "chat_messages": []}]
        self.assertIsInstance(detect(both, "conversations.json"),
                              ChatGPTImporter)


class TestChatGPTRich(unittest.TestCase):
    def test_timestamps_attachments_and_roles(self):
        convo = {"title": "Garden",
                 "mapping": {
                     "a": _mapping_user("I grow heirloom tomatoes.",
                                        1741000000.0),
                     "b": {"message": {"author": {"role": "assistant"},
                                       "content": {"parts": ["Nice!"]}}},
                     "c": {"message": {
                         "author": {"role": "user"},
                         "content": {"parts": [
                             "See the layout.",
                             {"content_type": "image",
                              "name": "plan.png"}]},
                         "create_time": 1741000060.0}}}}
        result = ChatGPTImporter().parse_data([convo], "conversations.json")
        self.assertEqual(result.provider, "chatgpt")
        self.assertEqual(len(result.candidates), 2)
        first, second = result.candidates
        self.assertEqual(first.source, "chatgpt:Garden")
        self.assertEqual(first.source_time, "2025-03-03T11:06:40Z")
        self.assertEqual(second.source_time, "2025-03-03T11:07:40Z")
        self.assertEqual(result.attachments, 1)
        self.assertEqual(second.attachments, ("plan.png",))

    def test_messages_fallback_shape(self):
        convo = {"title": "fb",
                 "messages": [{"role": "user", "content": "Fallback fact."},
                              {"role": "assistant",
                               "content": "Ignored."}]}
        result = ChatGPTImporter().parse_data([convo], "x.json")
        self.assertEqual(
            [(c.content, c.source) for c in result.candidates],
            [("Fallback fact.", "chatgpt:fb")])


class TestClaude(unittest.TestCase):
    def test_conversations_human_only(self):
        raw = (HERE / "fixtures" / "claude_export.json").read_bytes()
        result = import_bytes(raw, "conversations.json")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(len(result.candidates), 4)
        sources = {c.source for c in result.candidates}
        self.assertEqual(sources, {"claude:Sourdough schedule",
                                   "claude:Side project"})
        self.assertTrue(all("Lovely" not in c.content
                            and "Fun!" not in c.content
                            for c in result.candidates))
        by_content = {c.content: c for c in result.candidates}
        self.assertEqual(
            by_content["I bake sourdough every Sunday morning."].source_time,
            "2026-03-02T10:00:00Z")
        resume = by_content["I worked at Northwind Traders for six years."]
        self.assertEqual(resume.category, "experience")
        self.assertEqual(resume.attachments, ("resume.pdf",))
        self.assertEqual(result.attachments, 1)

    def test_memories_and_projects(self):
        importer = ClaudeImporter()
        mem = importer.parse_data(
            {"conversations_memory": "Owner likes tea.",
             "project_memories": {"p1": "Treehouse budget $500."}},
            "memories.json")
        self.assertEqual(len(mem.candidates), 2)
        proj = importer.parse_data(
            [{"name": "Cabin", "description": "Off-grid cabin plan.",
              "docs": [{"title": "solar", "content": "Panels face south."}]}],
            "projects.json")
        self.assertEqual(len(proj.candidates), 2)
        self.assertTrue(all(c.source.startswith("claude:project:Cabin")
                            for c in proj.candidates))


class TestGemini(unittest.TestCase):
    def test_prompts_only_with_day_sources(self):
        raw = (HERE / "fixtures" / "gemini_takeout.json").read_bytes()
        result = import_bytes(raw, "MyActivity.json")
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(
            [(c.content, c.source) for c in result.candidates],
            [("what soil suits balcony tomatoes?", "gemini:2026-05-01"),
             ("how do I fix a leaky tap?", "gemini:2026-05-02")])
        self.assertEqual(result.candidates[0].source_time,
                         "2026-05-01T08:00:00.000Z")
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.attachments, 1)

    def test_non_gemini_records_skipped(self):
        result = GeminiImporter().parse_data(
            [{"header": "YouTube", "title": "Prompted x"}], "MyActivity.json")
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.skipped, 1)


class TestGeneric(unittest.TestCase):
    def test_envelope_with_categories(self):
        result = GenericImporter().parse_data(
            {"memories": [
                {"content": "I prefer morning meetings.",
                 "source": "hand", "time": "2026-01-01"}]},
            "hand.json")
        (candidate,) = result.candidates
        self.assertEqual(
            (candidate.source, candidate.category, candidate.source_time),
            ("hand", "preference", "2026-01-01"))


def _zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class TestZipArchives(unittest.TestCase):
    def test_multi_member_merge(self):
        claude = (HERE / "fixtures" / "claude_export.json").read_bytes()
        payload = _zip_bytes({
            "account/conversations.json": claude,
            "account/memories.json": json.dumps(
                {"conversations_memory": "Owner keeps bees."}).encode(),
            "account/readme.txt": b"ignore me",
        })
        members = extract_members(payload)
        self.assertEqual(len(members), 2)
        result = import_upload("export.zip", payload)
        self.assertEqual(result.provider, "claude")
        self.assertEqual(len(result.candidates), 5)
        self.assertTrue(any(c.content == "Owner keeps bees."
                            for c in result.candidates))

    def test_takeout_picks_gemini_only(self):
        gemini = (HERE / "fixtures" / "gemini_takeout.json").read_bytes()
        payload = _zip_bytes({
            "Takeout/My Activity/YouTube/MyActivity.json": b"[]",
            "Takeout/My Activity/Gemini Apps/MyActivity.json": gemini,
        })
        members = extract_members(payload)
        self.assertEqual(len(members), 1)
        self.assertIn("Gemini", members[0][0])
        result = import_upload("takeout.zip", payload)
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(len(result.candidates), 2)

    def test_bad_and_empty_archives(self):
        with self.assertRaises(MemoryError):
            extract_members(b"not a zip")
        with self.assertRaises(MemoryError):
            import_upload("empty.zip", _zip_bytes({"notes.txt": b"x"}))

    def test_oversize_member_refused(self):
        big = b"x" * (5 * 1024 * 1024 + 1)
        payload = _zip_bytes({"conversations.json": big})
        with self.assertRaises(MemoryError):
            extract_members(payload)

    def test_too_many_members_refused(self):
        payload = _zip_bytes({f"f{i}.json": b"[]"
                              for i in range(51)})
        with self.assertRaises(MemoryError):
            extract_members(payload)

    def test_non_json_non_zip_rejected(self):
        with self.assertRaises(MemoryError):
            import_upload("export.txt", b"[]")


class TestStreaming(unittest.TestCase):
    def test_items_objects_strings_scalars(self):
        items = list(iter_json_array_items(
            io.BytesIO(b'[{"a": 1}, "s", 42, true, null]')))
        raws = [raw for raw, _ in items]
        self.assertEqual(len(raws), 5)
        self.assertEqual(json.loads(raws[0]), {"a": 1})
        self.assertEqual(json.loads(raws[1]), "s")

    def test_braces_inside_strings_ignored(self):
        items = list(iter_json_array_items(
            io.BytesIO(b'[{"t": "} {[ \\""}, {"t": 2}]')))
        self.assertEqual(len(items), 2)

    def test_truncated_and_non_array(self):
        with self.assertRaises(MemoryError):
            list(iter_json_array_items(io.BytesIO(b'[{"a": 1}')))
        with self.assertRaises(MemoryError):
            list(iter_json_array_items(io.BytesIO(b'{"a": 1}')))

    def test_early_stop_skips_late_garbage(self):
        items = [{"title": f"t{i}", "mapping": {
            "a": _mapping_user(f"Unique fact number {i} about jute.")}}
            for i in range(120)]
        items.append("THIS IS NOT JSON{")
        raw = json.dumps(items).encode()
        result = import_bytes(raw, "conversations.json")
        # Stops at the cap; the trailing garbage is never decoded.
        self.assertEqual(len(result.candidates), MAX_CANDIDATES)
        self.assertEqual(result.provider, "chatgpt")


class TestClassify(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(classify("I prefer morning meetings."),
                         "preference")
        self.assertEqual(classify("I know how to weld steel frames."),
                         "skill")
        self.assertEqual(classify("I worked at Northwind for six years."),
                         "experience")
        self.assertEqual(classify("I am building a treehouse app."),
                         "project")
        self.assertEqual(classify("I enjoy night kayaking trips."),
                         "interest")
        self.assertEqual(classify("Keep it short and use bullet points."),
                         "style")
        self.assertEqual(classify("The sky was pale that Tuesday."),
                         "general")


class TestConflicts(unittest.TestCase):
    def test_near_dupes_flagged_identicals_not(self):
        from app.importers.types import Candidate

        first = Candidate("I grow heirloom tomatoes yearly.",
                          "chatgpt:a")
        second = Candidate("I grow heirloom tomatoes every year.",
                           "claude:b")
        third = Candidate("I grow heirloom tomatoes yearly.",
                          "gemini:c")
        warnings = find_conflicts([first, second])
        self.assertEqual(len(warnings), 1)
        self.assertIn("item 1", warnings[0])
        self.assertIn("item 2", warnings[0])
        self.assertEqual(find_conflicts([first, third]), [])

    def test_import_attaches_warnings(self):
        raw = json.dumps({"memories": [
            {"content": "I grow heirloom tomatoes yearly."},
            {"content": "I grow heirloom tomatoes every year."}]}).encode()
        result = import_bytes(raw, "hand.json")
        self.assertEqual(result.provider, "generic")
        self.assertEqual(len(result.warnings), 1)


class TestMetaAI(unittest.TestCase):
    def test_never_detects_parse_explains(self):
        importer = MetaAIImporter()
        self.assertFalse(importer.detect([], "meta.json"))
        with self.assertRaisesRegex(MemoryError, "supported yet"):
            importer.parse_data([], "meta.json")


class TestMemoryEngine(unittest.TestCase):
    def setUp(self):
        from app.db import insert_returning_id

        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "m2.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        user_id = insert_returning_id(
            self.conn,
            "INSERT INTO users (name, role, email) VALUES (?, ?, ?)",
            ("Tee", "member", "tee2@example.org"))
        self.twin_id = insert_returning_id(
            self.conn,
            "INSERT INTO twins (user_id, display_name, persona, tone,"
            " avatar_seed) VALUES (?, ?, ?, ?, ?)",
            (user_id, "Tee", "helpful", "neutral", "seed-2"))

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _columns(self, table):
        if driver_name(self.conn) == "postgres":
            rows = self.conn.execute(
                "SELECT column_name AS name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ?",
                (table,))
        else:
            rows = self.conn.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}

    def test_v9_columns_and_version(self):
        self.assertTrue({"category", "source_time"}
                        <= self._columns("memories"))
        version = self.conn.execute(
            "SELECT version FROM schema_version WHERE id = 1").fetchone()
        self.assertEqual(version["version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 12)

    def test_category_and_time_stored(self):
        mem_id = memories.create_memory(
            self.conn, twin_id=self.twin_id, content="I prefer tea.",
            source="hand", category="preference",
            source_time="2026-01-02T00:00:00Z")
        row = memories.get_memory(self.conn, mem_id)
        self.assertEqual((row["category"], row["source_time"]),
                         ("preference", "2026-01-02T00:00:00Z"))

    def test_unknown_category_folds_to_general(self):
        mem_id = memories.create_memory(
            self.conn, twin_id=self.twin_id, content="Just a fact.", category="bogus")
        row = memories.get_memory(self.conn, mem_id)
        self.assertEqual(row["category"], "general")

    def test_list_filter_by_category(self):
        memories.create_memory(self.conn, twin_id=self.twin_id, content="I prefer tea.",
                               category="preference")
        memories.create_memory(self.conn, twin_id=self.twin_id, content="Plain fact.")
        prefs = memories.list_memories(self.conn, self.twin_id, "pending", "preference")
        self.assertEqual(len(prefs), 1)
        every = memories.list_memories(self.conn, self.twin_id, "pending")
        self.assertEqual(len(every), 2)


class TestBackCompat(unittest.TestCase):
    def test_legacy_wrapper_unchanged(self):
        from app.import_pipeline import parse_chatgpt_export

        raw = (HERE / "fixtures" / "sample_export.json").read_bytes()
        candidates = parse_chatgpt_export(raw)
        self.assertGreater(len(candidates), 0)
        content, source = candidates[0]
        self.assertTrue(content.strip())
        self.assertTrue(source.startswith("chatgpt:"))

    def test_legacy_errors_unchanged(self):
        from app.import_pipeline import parse_chatgpt_export

        with self.assertRaisesRegex(MemoryError, "not valid UTF-8 JSON"):
            parse_chatgpt_export(b"\xff\xfe garbage")
        with self.assertRaisesRegex(MemoryError, "Unsupported file"):
            parse_chatgpt_export(b'{"nope": 1}')


if __name__ == "__main__":
    unittest.main()
