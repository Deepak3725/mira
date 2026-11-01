"""Restricted command runner: approval-gated, jailed, audited.

Safety model (honest limits included):
- Nothing runs without explicit owner approval per invocation.
- No shell: argv only, interpreter allowlist, temp-dir jail as cwd.
- Environment is scrubbed of secrets; output is capped; timeouts kill.
- Every run is described for the audit log by the caller.

Limitation: this is process isolation, not a container boundary. True
container isolation needs Docker, which is absent on this machine; the
runner documents that and stays refuse-by-default for anything
outside the allowlist.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

from .graph import ProjectError

#: Executable basenames allowed as argv[0] (resolved name, no paths).
ALLOWED_BINARIES = ("python", "python3", "pytest", "node")
DEFAULT_TIMEOUT_S = 60
MAX_OUTPUT_BYTES = 64 * 1024

_SCRUB_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD",
                  "DATABASE_URL", "MIRA_")


@dataclass(frozen=True)
class RunResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def scrub_env(env: dict) -> dict:
    """Copy env minus anything that smells like a secret."""
    clean = {}
    for key, value in env.items():
        upper = str(key).upper()
        if any(marker in upper for marker in _SCRUB_MARKERS):
            continue
        clean[key] = value
    return clean


def check_argv(argv: list[str]) -> str:
    """Validate an argv vector; returns the binary basename or raises."""
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise ProjectError("Empty or invalid command.")
    name = os.path.basename(argv[0]).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in ALLOWED_BINARIES:
        raise ProjectError(
            f"Command {argv[0]!r} is not on the runner allowlist "
            f"({', '.join(ALLOWED_BINARIES)}).")
    return name


def run(argv: list[str], *, approved: bool,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_OUTPUT_BYTES,
        stage: dict[str, str] | None = None,
        popen_factory=None) -> RunResult:
    """Run an allowlisted command in a fresh temp jail.

    ``stage`` maps relative paths to file contents placed in the jail
    first. ``popen_factory`` injects a fake Popen for tests.
    """
    if not approved:
        raise ProjectError("Owner approval is required before running.")
    check_argv(argv)
    if timeout_s <= 0 or timeout_s > 600:
        raise ProjectError("Timeout must be 1..600 seconds.")
    factory = popen_factory or subprocess.Popen
    with tempfile.TemporaryDirectory(prefix="mira-run-") as jail:
        for rel, body in (stage or {}).items():
            target = os.path.join(jail, rel)
            if not os.path.realpath(target).startswith(
                    os.path.realpath(jail) + os.sep):
                raise ProjectError(f"Unsafe stage path {rel!r}.")
            os.makedirs(os.path.dirname(target) or jail, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(body or "")
        proc = factory(argv, cwd=jail, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,
                       env=scrub_env(dict(os.environ)),
                       text=True, shell=False)
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                out, err = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001 - best effort after kill
                out, err = "", "killed after timeout"
            return RunResult(ok=False,
                             returncode=getattr(proc, "returncode", -1) or -1,
                             stdout=(out or "")[:max_bytes],
                             stderr=(err or "")[:max_bytes],
                             timed_out=True)
        code = proc.returncode if proc.returncode is not None else -1
        return RunResult(ok=code == 0, returncode=code,
                         stdout=(out or "")[:max_bytes],
                         stderr=(err or "")[:max_bytes],
                         timed_out=False)
