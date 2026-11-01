"""Phase 3 tests: project engine (DAG, spec, assign, workspace,
restricted runner, lifecycle, review, report, v10 migration)."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import (
    SCHEMA_VERSION,
    driver_name,
    get_connection,
    init_db,
    insert_returning_id,
)
from app.projects import (
    add_task,
    advance,
    approve_task,
    assign,
    cancel_task,
    check_argv,
    clarify_questions,
    clean_path,
    create_project,
    draft_spec,
    draft_with_ai,
    finalize,
    get_project,
    list_tasks,
    ready_set,
    request_review,
    retry_task,
    run,
    run_checks,
    save_spec,
    scrub_env,
    set_project_status,
    topo_sort,
)
from app.projects import workspace
from app.projects.graph import ProjectError
from app.projects.runner import RunResult


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "p3.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.owner = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email) VALUES (?, ?, ?)",
            ("Own", "member", "own3@example.org"))
        self.other = insert_returning_id(
            self.conn, "INSERT INTO users (name, role, email) VALUES (?, ?, ?)",
            ("Oth", "member", "oth3@example.org"))

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _project(self, goal="Build an e-commerce website."):
        return create_project(self.conn, self.owner, "Shop", goal)


class TestGraph(unittest.TestCase):
    def test_topo_orders_dependencies_first(self):
        order = topo_sort({1: [], 2: [1], 3: [1, 2]})
        self.assertLess(order.index(1), order.index(2))
        self.assertLess(order.index(2), order.index(3))

    def test_cycle_unknown_and_self_rejected(self):
        with self.assertRaisesRegex(ProjectError, "Circular"):
            topo_sort({1: [2], 2: [1]})
        with self.assertRaisesRegex(ProjectError, "unknown task"):
            topo_sort({1: [9]})
        with self.assertRaisesRegex(ProjectError, "itself"):
            topo_sort({1: [1]})

    def test_ready_set_parallel(self):
        tasks = {1: [], 2: [], 3: [1, 2]}
        self.assertEqual(ready_set(tasks, set()), [1, 2])
        self.assertEqual(ready_set(tasks, {1}), [2])
        self.assertEqual(ready_set(tasks, {1, 2}), [3])


class TestSpec(unittest.TestCase):
    def test_clarify(self):
        self.assertEqual(clarify_questions(""),
                         ["What should MIRA build? Describe the goal in a sentence."])
        short = clarify_questions("Build shop")
        self.assertTrue(any("finished result" in q for q in short))
        crisp = clarify_questions(
            "Build an e-commerce website for my bakery with checkout.")
        self.assertEqual(crisp, [])

    def test_draft_shape_and_gates(self):
        spec = draft_spec("Build an e-commerce website.",
                          {"audience": "my bakery"})
        self.assertEqual(spec["deliverable"], "website")
        self.assertEqual(len(spec["tasks"]), 5)
        design = next(t for t in spec["tasks"] if t["key"] == "design")
        self.assertTrue(design["needs_approval"])
        self.assertEqual(
            [t["key"] for t in spec["tasks"]],
            ["research", "design", "build", "test", "review"])


class TestDraftWithAI(ProjectCase):
    def test_mock_falls_back_to_draft(self):
        spec = draft_with_ai(self.conn, "Build a shop.", user_id=self.owner)
        self.assertEqual(spec["goal"], "Build a shop.")
        self.assertEqual(len(spec["tasks"]), 5)


class TestAssign(unittest.TestCase):
    def test_mock_default_and_images_refuse(self):
        provider, _model = assign("code")
        self.assertEqual(provider, "mock")
        with self.assertRaises(ProjectError):
            assign("image", allow_mock=False)

    def test_explicit_unknown_falls_to_mock(self):
        with self.assertLogs("mira", level="WARNING"):
            provider, _model = assign("code", explicit="nope")
        self.assertEqual(provider, "mock")


class TestWorkspace(ProjectCase):
    def test_versions_never_overwrite(self):
        pid = self._project()
        self.assertEqual(workspace.write_file(
            self.conn, pid, "index.html", "<v1>"), 1)
        self.assertEqual(workspace.write_file(
            self.conn, pid, "index.html", "<v2>"), 2)
        latest = workspace.read_file(self.conn, pid, "index.html")
        self.assertEqual((latest["version"], latest["content"]),
                         (2, "<v2>"))
        first = workspace.read_file(self.conn, pid, "index.html", 1)
        self.assertEqual(first["content"], "<v1>")
        self.assertEqual(len(workspace.history(self.conn, pid,
                                               "index.html")), 2)
        self.assertEqual(
            [f["path"] for f in workspace.list_files(self.conn, pid)],
            ["index.html"])

    def test_unsafe_paths_and_oversize(self):
        pid = self._project()
        for bad in ["", "../x", "/abs", "a/../../b", "x" * 201]:
            with self.assertRaises(ProjectError):
                clean_path(bad)
        with self.assertRaises(ProjectError):
            workspace.write_file(self.conn, pid, "big.txt", "x" * 200_001)


class FakePopen:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = 0
        self.killed = False

    def communicate(self, timeout=None):
        return "STDOUT", "STDERR"

    def kill(self):
        self.killed = True
        self.returncode = -1


class TestRunner(unittest.TestCase):
    def test_approval_and_allowlist(self):
        with self.assertRaisesRegex(ProjectError, "approval"):
            run(["python", "--version"], approved=False)
        with self.assertRaisesRegex(ProjectError, "allowlist"):
            run(["rm", "-rf", "/"], approved=True)
        with self.assertRaises(ProjectError):
            run([], approved=True)
        self.assertEqual(check_argv(["pytest", "-q"]), "pytest")

    def test_success_and_staging(self):
        seen = {}

        class Spy(FakePopen):
            def communicate(self, timeout=None):
                import os as _os
                seen["cwd"] = self.kwargs.get("cwd")
                seen["files"] = sorted(_os.listdir(seen["cwd"]))
                return "ok", ""

        result = run(["python", "--version"], approved=True,
                     stage={"main.py": "print(1)"}, popen_factory=Spy)
        self.assertTrue(result.ok)
        self.assertEqual((result.stdout, result.timed_out), ("ok", False))
        self.assertEqual(seen["files"], ["main.py"])
        with self.assertRaises(ProjectError):
            run(["python", "x"], approved=True,
                stage={"../evil.py": "x"}, popen_factory=FakePopen)

    def test_timeout_and_caps(self):
        class Slow(FakePopen):
            def communicate(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(self.argv, timeout)
                return "partial", "killed"

        result = run(["python", "x"], approved=True, popen_factory=Slow)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

        class Loud(FakePopen):
            def communicate(self, timeout=None):
                return "y" * 100, ""

        result = run(["python", "x"], approved=True, max_bytes=10,
                     popen_factory=Loud)
        self.assertEqual(result.stdout, "y" * 10)

    def test_env_scrubbed(self):
        captured = {}

        class EnvSpy(FakePopen):
            def communicate(self, timeout=None):
                captured.update(self.kwargs.get("env", {}))
                return "", ""

        with mock.patch.dict("os.environ", {"MODEL_API_KEY": "k",
                                            "DATABASE_URL": "u",
                                            "KEEP_ME": "1"}):
            run(["python", "x"], approved=True, popen_factory=EnvSpy)
        self.assertNotIn("MODEL_API_KEY", captured)
        self.assertNotIn("DATABASE_URL", captured)
        self.assertEqual(captured.get("KEEP_ME"), "1")
        self.assertNotIn("MIRA_TEST_DATABASE_URL",
                         scrub_env({"MIRA_TEST_DATABASE_URL": "x"}))


def _ok_executor(result=None):
    def run_it(conn, user_id, project, task):
        return result if result is not None else {
            "summary": f"did {task['title']}", "files": {}}
    return run_it


class TestEngine(ProjectCase):
    def test_ownership_enforced(self):
        pid = self._project()
        self.assertIsNone(get_project(self.conn, pid, self.other))
        with self.assertRaises(ProjectError):
            add_task(self.conn, pid, self.other, title="X")
        with self.assertRaises(ProjectError):
            advance(self.conn, pid, self.other, user_id=self.other,
                    executor=_ok_executor())

    def test_add_task_validation(self):
        pid = self._project()
        with self.assertRaises(ProjectError):
            add_task(self.conn, pid, self.owner, title="")
        with self.assertRaises(ProjectError):
            add_task(self.conn, pid, self.owner, kind="bogus",
                     title="X")
        with self.assertRaisesRegex(ProjectError, "unknown task"):
            add_task(self.conn, pid, self.owner, title="X", deps=[999])

    def test_save_spec_replaces_and_orders(self):
        pid = self._project()
        add_task(self.conn, pid, self.owner, title="stale")
        ids = save_spec(self.conn, pid, self.owner,
                        draft_spec("Build a shop."))
        self.assertEqual(len(ids), 5)
        tasks = list_tasks(self.conn, pid)
        self.assertEqual(
            [t["title"] for t in tasks],
            ["Research requirements", "Write the design",
             "Build the deliverable", "Test the deliverable",
             "Review and integrate"])
        with self.assertRaises(ProjectError):
            save_spec(self.conn, pid, self.owner,
                      {"tasks": [{"key": "a", "title": "A",
                                  "deps": ["b"]},
                                 {"key": "b", "title": "B",
                                  "deps": ["a"]}]})

    def test_advance_runs_dag_with_files(self):
        pid = self._project()
        first = add_task(self.conn, pid, self.owner, kind="writing",
                         title="Draft")
        second = add_task(self.conn, pid, self.owner, kind="code",
                          title="Build", deps=[first])

        def build(conn, user_id, project, task):
            return {"summary": "built",
                    "files": {"index.html": "<h1>shop</h1>"}}

        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner, executor=build)
        self.assertEqual(outcomes,
                         [{"id": first, "status": "done",
                           "provider": "mock"}])
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner, executor=build)
        self.assertEqual(outcomes[0]["status"], "done")
        latest = workspace.read_file(self.conn, pid, "index.html")
        self.assertEqual((latest["version"], latest["updated_by"]),
                         (2, f"task-{second}"))

    def test_approval_gate_and_review_loop(self):
        pid = self._project()
        gated = add_task(self.conn, pid, self.owner, title="Design",
                         needs_approval=True)
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner,
                           executor=_ok_executor())
        self.assertEqual(outcomes, [])
        approve_task(self.conn, pid, gated, self.owner)
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner,
                           executor=_ok_executor())
        self.assertEqual(outcomes[0]["status"], "done")
        request_review(self.conn, pid, gated, self.owner, note="check")
        approve_task(self.conn, pid, gated, self.owner)
        task = next(t for t in list_tasks(self.conn, pid)
                    if t["id"] == gated)
        self.assertEqual(task["status"], "done")

    def test_failure_routing_and_retry(self):
        pid = self._project()
        first = add_task(self.conn, pid, self.owner, title="Fragile")
        second = add_task(self.conn, pid, self.owner, title="Later",
                          deps=[first])

        def boom(conn, user_id, project, task):
            raise RuntimeError("agent exploded")

        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner, executor=boom)
        self.assertEqual(outcomes[0]["status"], "failed")
        blocked = advance(self.conn, pid, self.owner,
                          user_id=self.owner,
                          executor=_ok_executor())
        self.assertEqual(blocked, [])
        retry_task(self.conn, pid, first, self.owner)
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner,
                           executor=_ok_executor())
        self.assertEqual(outcomes[0]["status"], "done")
        task = next(t for t in list_tasks(self.conn, pid)
                    if t["id"] == first)
        self.assertEqual(task["attempts"], 2)
        self.assertEqual(task["error"], "")

    def test_pause_resume_cancel(self):
        pid = self._project()
        add_task(self.conn, pid, self.owner, title="T")
        set_project_status(self.conn, pid, self.owner, "paused")
        with self.assertRaisesRegex(ProjectError, "resume"):
            advance(self.conn, pid, self.owner, user_id=self.owner,
                    executor=_ok_executor())
        set_project_status(self.conn, pid, self.owner, "active")
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner,
                           executor=_ok_executor())
        self.assertEqual(outcomes[0]["status"], "done")
        set_project_status(self.conn, pid, self.owner, "cancelled")
        with self.assertRaises(ProjectError):
            add_task(self.conn, pid, self.owner, title="Late")

    def test_finalize_report(self):
        pid = self._project()
        tid = add_task(self.conn, pid, self.owner, title="Only")
        with self.assertRaisesRegex(ProjectError, "still open"):
            finalize(self.conn, pid, self.owner)
        advance(self.conn, pid, self.owner, user_id=self.owner,
                executor=_ok_executor(
                    {"summary": "s", "files": {"a.txt": "A"}}))
        report = finalize(self.conn, pid, self.owner)
        self.assertEqual(report["title"], "Shop")
        self.assertEqual(report["tasks"],
                         [{"id": tid, "title": "Only", "status": "done",
                           "provider": "mock", "attempts": 1}])
        self.assertEqual(report["deliverables"],
                         [{"path": "a.txt", "version": 1,
                           "updated_by": f"task-{tid}"}])
        self.assertTrue(report["events"])

    def test_run_checks_gated_and_audited(self):
        pid = self._project()
        with self.assertRaisesRegex(ProjectError, "approval"):
            run_checks(self.conn, pid, self.owner, argv=["python", "-V"],
                       approved=False)
        with mock.patch("app.projects.engine.run_command") as fake:
            fake.return_value = RunResult(True, 0, "ok", "", False)
            result = run_checks(self.conn, pid, self.owner,
                                argv=["python", "-V"], approved=True)
        self.assertTrue(result.ok)
        fake.assert_called_once()
        staged = fake.call_args.kwargs.get("stage")
        self.assertEqual(staged, {})

    def test_mock_end_to_end(self):
        from app.projects.engine import execute_with_ai

        pid = self._project()
        save_spec(self.conn, pid, self.owner,
                  {"tasks": [{"key": "a", "kind": "writing",
                              "title": "Write it",
                              "instructions": "Write hello."}]})
        outcomes = advance(self.conn, pid, self.owner,
                           user_id=self.owner,
                           executor=execute_with_ai)
        self.assertEqual(outcomes[0]["status"], "done")
        report = finalize(self.conn, pid, self.owner)
        self.assertEqual(len(report["tasks"]), 1)


class TestMigrationV10(ProjectCase):
    def test_project_tables_and_version(self):
        if driver_name(self.conn) == "postgres":
            rows = self.conn.execute(
                "SELECT table_name AS name FROM information_schema.tables"
                " WHERE table_schema = current_schema()")
            tables = {r["name"] for r in rows}
        else:
            rows = self.conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table'")
            tables = {r["name"] for r in rows}
        self.assertTrue({"projects", "project_tasks", "project_files",
                         "project_events"} <= tables)
        version = self.conn.execute(
            "SELECT version FROM schema_version WHERE id = 1").fetchone()
        self.assertEqual(version["version"], SCHEMA_VERSION)


class TestProjectRouteWiring(unittest.TestCase):
    def test_post_handlers_guarded(self):
        from pathlib import Path as _Path

        text = (_Path(__file__).resolve().parent.parent
                / "app" / "routes" / "projects.py").read_text(
                    encoding="utf-8")
        handlers = ["project_create", "project_spec", "project_add_task",
                    "project_advance", "project_approve_task",
                    "project_retry_task", "project_cancel_task",
                    "project_review_task", "project_status",
                    "project_write_file", "project_checks",
                    "project_finalize"]
        for name in handlers:
            start = text.find(f"def {name}(")
            self.assertGreaterEqual(start, 0, name)
            rest = text[start:]
            for marker in ("\n@router.", "\ndef "):
                cut = rest.find(marker, 1)
                if cut > 0:
                    rest = rest[:cut]
            self.assertIn("require_session_csrf", rest, name)
            self.assertIn("_limited", rest, name)
            if name != "project_create":
                self.assertIn("_own_project_or_404", rest, name)


if __name__ == "__main__":
    unittest.main()
