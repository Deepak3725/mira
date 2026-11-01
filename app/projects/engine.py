"""Project orchestration: lifecycle, DAG execution, review, report.

All mutating functions take the owner's id and refuse cross-owner
access (engine-level ownership; routes pass the session user).
Task execution goes through an injected executor so tests use fakes
and production uses `execute_with_ai` (Phase 1 orchestrator).
"""

from __future__ import annotations

import json

from ..ai import new_project as new_ai_project
from ..ai import new_task as new_ai_task
from ..ai import run_task
from ..db import driver_name, insert_returning_id
from . import workspace
from .assign import assign
from .graph import ProjectError, ready_set, topo_sort
from .runner import RunResult, run as run_command

PROJECT_STATUSES = ("draft", "active", "paused", "done", "cancelled")
TASK_STATUSES = ("pending", "ready", "running", "failed", "needs_review",
                 "done", "cancelled", "paused")
TASK_KINDS = ("chat", "writing", "code", "research", "analysis", "test",
              "review", "intent", "image")


def _load(raw: str, default):
    try:
        return json.loads(raw or "")
    except ValueError:
        return default


def _dump(value) -> str:
    return json.dumps(value, default=str)


def _touch(conn, table: str, row_id: int) -> None:
    stamp = "now()" if driver_name(conn) == "postgres" else "datetime('now')"
    conn.execute(f"UPDATE {table} SET updated_at = {stamp} WHERE id = ?",
                 (row_id,))


def log_event(conn, project_id: int, actor: str, action: str,
              detail: str = "") -> None:
    conn.execute(
        "INSERT INTO project_events (project_id, actor, action, detail)"
        " VALUES (?, ?, ?, ?)",
        (project_id, (actor or "")[:120], action,
         (detail or "")[:2000]))


def create_project(conn, owner_user_id: int, title: str,
                   goal: str = "") -> int:
    title = (title or "").strip()
    if not title:
        raise ProjectError("Projects need a title.")
    pid = insert_returning_id(
        conn,
        "INSERT INTO projects (owner_user_id, title, goal)"
        " VALUES (?, ?, ?)",
        (owner_user_id, title[:200], (goal or "")[:4000]))
    log_event(conn, pid, f"user:{owner_user_id}", "project.created", title)
    return pid


def get_project(conn, project_id: int, owner_user_id: int):
    row = conn.execute("SELECT * FROM projects WHERE id = ?",
                       (project_id,)).fetchone()
    if row is None or row["owner_user_id"] != owner_user_id:
        return None
    return row


def list_projects(conn, owner_user_id: int) -> list:
    return list(conn.execute(
        "SELECT * FROM projects WHERE owner_user_id = ? ORDER BY id DESC",
        (owner_user_id,)))


def get_task(conn, project_id: int, task_id: int):
    return conn.execute("SELECT * FROM project_tasks WHERE id = ?"
                        " AND project_id = ?",
                        (task_id, project_id)).fetchone()


def list_tasks(conn, project_id: int) -> list:
    return list(conn.execute("SELECT * FROM project_tasks"
                             " WHERE project_id = ? ORDER BY id",
                             (project_id,)))


def _validate_deps(conn, project_id: int, deps: list[int],
                   exclude: int | None = None) -> None:
    rows = [r for r in list_tasks(conn, project_id)
            if r["id"] != exclude]
    graph = {r["id"]: [d for d in _load(r["deps_json"], [])
                       if isinstance(d, int)] for r in rows}
    probe = max([r["id"] for r in rows] + [0]) + 1
    graph[probe] = [d for d in deps if isinstance(d, int)]
    topo_sort(graph)  # raises on unknown deps, self-deps, cycles


def add_task(conn, project_id: int, owner_user_id: int, *,
             kind: str = "chat", title: str, instructions: str = "",
             deps: list[int] | None = None,
             needs_approval: bool = False) -> int:
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    if project["status"] in ("done", "cancelled"):
        raise ProjectError("Finished projects cannot change.")
    title = (title or "").strip()
    if not title:
        raise ProjectError("Tasks need a title.")
    kind = (kind or "chat").lower()
    if kind not in TASK_KINDS:
        raise ProjectError(f"Unknown task kind {kind!r}.")
    dep_ids = [d for d in (deps or []) if isinstance(d, int)]
    _validate_deps(conn, project_id, dep_ids)
    provider, model = "", ""
    try:
        provider, model = assign(kind)
    except ProjectError:
        pass  # assignment retried at run time; creation still valid
    tid = insert_returning_id(
        conn,
        "INSERT INTO project_tasks (project_id, kind, title, instructions,"
        " deps_json, provider, model, needs_approval)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, kind, title[:200], (instructions or "")[:8000],
         _dump(dep_ids), provider, model, 1 if needs_approval else 0))
    log_event(conn, project_id, f"user:{owner_user_id}", "task.added",
              f"#{tid} {title}")
    refresh_ready(conn, project_id)
    return tid


def save_spec(conn, project_id: int, owner_user_id: int,
              spec: dict) -> list[int]:
    """Replace the project's task list from a spec dict (draft only)."""
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    if project["status"] != "draft":
        raise ProjectError("Specs can only change while drafting.")
    if not isinstance(spec, dict) or not isinstance(
            spec.get("tasks"), list) or not spec["tasks"]:
        raise ProjectError("Spec has no tasks.")
    conn.execute("DELETE FROM project_tasks WHERE project_id = ?",
                 (project_id,))
    key_to_id: dict[str, int] = {}
    for index, item in enumerate(spec["tasks"]):
        if not isinstance(item, dict) or not item.get("title"):
            raise ProjectError(f"Spec task {index} has no title.")
        key = str(item.get("key") or f"task-{index}")
        tid = insert_returning_id(
            conn,
            "INSERT INTO project_tasks (project_id, kind, title,"
            " instructions, deps_json, needs_approval)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, str(item.get("kind") or "chat").lower(),
             str(item["title"])[:200],
             str(item.get("instructions") or "")[:8000], "[]",
             1 if item.get("needs_approval") else 0))
        key_to_id[key] = tid
    for item in spec["tasks"]:
        key = str(item.get("key") or "")
        deps = []
        for dep in item.get("deps") or []:
            if str(dep) not in key_to_id:
                raise ProjectError(f"Unknown spec dependency {dep!r}.")
            deps.append(key_to_id[str(dep)])
        conn.execute("UPDATE project_tasks SET deps_json = ? WHERE id = ?",
                     (_dump(deps), key_to_id[key]))
    rows = list_tasks(conn, project_id)
    graph = {r["id"]: _load(r["deps_json"], []) for r in rows}
    topo_sort(graph)  # cycle check before committing to the shape
    conn.execute("UPDATE projects SET spec_json = ? WHERE id = ?",
                 (_dump(spec)[:20000], project_id))
    _touch(conn, "projects", project_id)
    log_event(conn, project_id, f"user:{owner_user_id}", "spec.saved",
              f"{len(rows)} tasks")
    refresh_ready(conn, project_id)
    return [r["id"] for r in rows]


def refresh_ready(conn, project_id: int) -> list[int]:
    """Move unblocked pending tasks to ready. Returns ready ids."""
    rows = list_tasks(conn, project_id)
    live = [r for r in rows if r["status"] not in ("cancelled", "paused")]
    graph = {r["id"]: [d for d in _load(r["deps_json"], [])
                       if isinstance(d, int)] for r in live}
    finished = {r["id"] for r in live if r["status"] == "done"}
    ready = []
    for tid in ready_set(graph, finished):
        row = next(r for r in live if r["id"] == tid)
        if row["status"] == "pending" and not row["needs_approval"]:
            conn.execute("UPDATE project_tasks SET status = 'ready'"
                         " WHERE id = ?", (tid,))
            _touch(conn, "project_tasks", tid)
            ready.append(tid)
        elif row["status"] == "ready":
            ready.append(tid)
    return ready


def set_project_status(conn, project_id: int, owner_user_id: int,
                       status: str) -> None:
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    if status not in PROJECT_STATUSES:
        raise ProjectError(f"Unknown project status {status!r}.")
    if project["status"] in ("done", "cancelled") and status != project[
            "status"]:
        raise ProjectError("Finished projects cannot change.")
    conn.execute("UPDATE projects SET status = ? WHERE id = ?",
                 (status, project_id))
    _touch(conn, "projects", project_id)
    log_event(conn, project_id, f"user:{owner_user_id}",
              f"project.{status}")


def approve_task(conn, project_id: int, task_id: int,
                 owner_user_id: int) -> None:
    """Approve a gated task (pending approval) or a review."""
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    task = get_task(conn, project_id, task_id)
    if task is None:
        raise ProjectError("Task not found.")
    if task["status"] == "needs_review":
        conn.execute("UPDATE project_tasks SET status = 'done' WHERE id = ?",
                     (task_id,))
    elif task["needs_approval"]:
        conn.execute("UPDATE project_tasks SET needs_approval = 0"
                     " WHERE id = ?", (task_id,))
    else:
        raise ProjectError("Task needs no approval.")
    _touch(conn, "project_tasks", task_id)
    log_event(conn, project_id, f"user:{owner_user_id}", "task.approved",
              f"#{task_id}")
    refresh_ready(conn, project_id)


def request_review(conn, project_id: int, task_id: int,
                   owner_user_id: int, note: str = "") -> None:
    task = _owned_task(conn, project_id, task_id, owner_user_id)
    if task["status"] != "done":
        raise ProjectError("Only finished tasks can go to review.")
    conn.execute("UPDATE project_tasks SET status = 'needs_review',"
                 " error = ? WHERE id = ?", ((note or "")[:500], task_id))
    _touch(conn, "project_tasks", task_id)
    log_event(conn, project_id, f"user:{owner_user_id}",
              "task.review_requested", f"#{task_id}")


def retry_task(conn, project_id: int, task_id: int,
               owner_user_id: int) -> None:
    task = _owned_task(conn, project_id, task_id, owner_user_id)
    if task["status"] not in ("failed", "needs_review"):
        raise ProjectError("Only failed or review tasks retry.")
    conn.execute("UPDATE project_tasks SET status = 'pending', error = ''"
                 " WHERE id = ?", (task_id,))
    _touch(conn, "project_tasks", task_id)
    log_event(conn, project_id, f"user:{owner_user_id}", "task.retried",
              f"#{task_id}")
    refresh_ready(conn, project_id)


def cancel_task(conn, project_id: int, task_id: int,
                owner_user_id: int) -> None:
    _owned_task(conn, project_id, task_id, owner_user_id)
    conn.execute("UPDATE project_tasks SET status = 'cancelled'"
                 " WHERE id = ?", (task_id,))
    _touch(conn, "project_tasks", task_id)
    log_event(conn, project_id, f"user:{owner_user_id}", "task.cancelled",
              f"#{task_id}")
    refresh_ready(conn, project_id)


def _owned_task(conn, project_id: int, task_id: int, owner_user_id: int):
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    task = get_task(conn, project_id, task_id)
    if task is None:
        raise ProjectError("Task not found.")
    return task


def execute_with_ai(conn, user_id: int, project: dict,
                    task: dict) -> dict:
    """Default executor: one task through the AI orchestrator."""
    ai_project = new_ai_project(f"project-{project['id']}",
                                project["goal"] or project["title"])
    ai_task = new_ai_task(ai_project, f"task-{task['id']}",
                          task["kind"], task["instructions"])
    result = run_task(
        conn, ai_task,
        messages=[{"role": "user",
                   "content": task["instructions"][:4000]}],
        user_id=user_id)
    return {"summary": result.text[:2000], "provider": result.provider,
            "model": result.model, "files": {}}


def advance(conn, project_id: int, owner_user_id: int, *,
            user_id: int, executor=None,
            max_tasks: int = 1) -> list[dict]:
    """Run up to max_tasks ready tasks. Returns per-task outcomes."""
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    if project["status"] == "draft":
        set_project_status(conn, project_id, owner_user_id, "active")
        project = get_project(conn, project_id, owner_user_id)
    if project["status"] != "active":
        raise ProjectError(
            f"Project is {project['status']}; resume it first.")
    run = executor or execute_with_ai
    outcomes = []
    for _ in range(max(1, max_tasks)):
        ready = refresh_ready(conn, project_id)
        if not ready:
            break
        task = get_task(conn, project_id, ready[0])
        conn.execute("UPDATE project_tasks SET status = 'running',"
                     " attempts = attempts + 1 WHERE id = ?",
                     (task["id"],))
        _touch(conn, "project_tasks", task["id"])
        provider, model = task["provider"], task["model"]
        try:
            if not provider:
                provider, model = assign(task["kind"])
                conn.execute("UPDATE project_tasks SET provider = ?,"
                             " model = ? WHERE id = ?",
                             (provider, model, task["id"]))
            output = run(conn, user_id, dict(project), dict(task))
            if not isinstance(output, dict):
                raise ProjectError("Executor must return a dict.")
            for path, body in (output.get("files") or {}).items():
                workspace.write_file(conn, project_id, str(path),
                                     str(body or ""),
                                     updated_by=f"task-{task['id']}")
            conn.execute("UPDATE project_tasks SET status = 'done',"
                         " result_json = ?, error = '' WHERE id = ?",
                         (_dump(output)[:20000], task["id"]))
            _touch(conn, "project_tasks", task["id"])
            log_event(conn, project_id, f"task-{task['id']}",
                      "task.done",
                      f"#{task['id']} via {provider}/{model}")
            outcomes.append({"id": task["id"], "status": "done",
                             "provider": provider})
        except Exception as exc:  # noqa: BLE001 - failure routing is data
            conn.execute("UPDATE project_tasks SET status = 'failed',"
                         " error = ? WHERE id = ?",
                         (f"{type(exc).__name__}: {exc}"[:500],
                          task["id"]))
            _touch(conn, "project_tasks", task["id"])
            log_event(conn, project_id, f"task-{task['id']}",
                      "task.failed",
                      f"#{task['id']}: {exc}"[:500])
            outcomes.append({"id": task["id"], "status": "failed",
                             "error": str(exc)[:200]})
    refresh_ready(conn, project_id)
    return outcomes


def run_checks(conn, project_id: int, owner_user_id: int, *,
               argv: list[str], approved: bool,
               timeout_s: int = 60) -> RunResult:
    """Approval-gated test run over the latest workspace files."""
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    stage = {f["path"]: f["content"]
             for f in workspace.list_files(conn, project_id)}
    result = run_command(argv, approved=approved, timeout_s=timeout_s,
                         stage=stage)
    log_event(conn, project_id, f"user:{owner_user_id}", "checks.run",
              f"$ {' '.join(argv)[:200]} -> {result.returncode}"
              f"{' (timeout)' if result.timed_out else ''}")
    return result


def finalize(conn, project_id: int, owner_user_id: int) -> dict:
    """Final report + deliverables; marks done when all tasks settled."""
    project = get_project(conn, project_id, owner_user_id)
    if project is None:
        raise ProjectError("Project not found.")
    tasks = list_tasks(conn, project_id)
    open_tasks = [t for t in tasks
                  if t["status"] not in ("done", "cancelled")]
    if open_tasks:
        raise ProjectError(
            f"{len(open_tasks)} tasks still open; finish, approve, or"
            " cancel them first.")
    set_project_status(conn, project_id, owner_user_id, "done")
    files = workspace.list_files(conn, project_id)
    events = list(conn.execute(
        "SELECT actor, action, detail, created_at FROM project_events"
        " WHERE project_id = ? ORDER BY id DESC LIMIT 50",
        (project_id,)))
    return {
        "title": project["title"], "goal": project["goal"],
        "tasks": [{"id": t["id"], "title": t["title"],
                   "status": t["status"], "provider": t["provider"],
                   "attempts": t["attempts"]}
                  for t in tasks],
        "deliverables": [{"path": f["path"], "version": f["version"],
                          "updated_by": f["updated_by"]} for f in files],
        "events": [dict(e) for e in events],
    }


def recent_events(conn, project_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM project_events WHERE project_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (project_id, max(1, min(limit, 200))))
    return [dict(r) for r in rows]
