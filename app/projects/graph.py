"""Task graph (DAG): ordering, cycle detection, parallel-ready sets."""

from __future__ import annotations


class ProjectError(Exception):
    """User-facing project-engine error."""


def topo_sort(tasks: dict[int, list[int]]) -> list[int]:
    """Order task ids so every dependency runs first.

    ``tasks`` maps id -> dependency ids. Unknown dependency ids and
    self-dependencies raise ProjectError, as do cycles (named).
    Deterministic: ties break by ascending id.
    """
    for tid, deps in tasks.items():
        if tid in deps:
            raise ProjectError(f"Task {tid} depends on itself.")
        for dep in deps:
            if dep not in tasks:
                raise ProjectError(
                    f"Task {tid} depends on unknown task {dep}.")
    ordered: list[int] = []
    visiting: set[int] = set()
    done: set[int] = set()

    def visit(tid: int, trail: list[int]) -> None:
        if tid in done:
            return
        if tid in visiting:
            cycle = " -> ".join(
                str(t) for t in [*trail, tid])
            raise ProjectError(f"Circular dependency: {cycle}.")
        visiting.add(tid)
        for dep in sorted(tasks[tid]):
            visit(dep, [*trail, tid])
        visiting.discard(tid)
        done.add(tid)
        ordered.append(tid)

    for tid in sorted(tasks):
        visit(tid, [])
    return ordered


def ready_set(tasks: dict[int, list[int]],
              finished: set[int]) -> list[int]:
    """Ids whose dependencies all finished (may run in parallel)."""
    ready = [tid for tid, deps in tasks.items()
             if tid not in finished
             and all(d in finished for d in deps)]
    return sorted(ready)
