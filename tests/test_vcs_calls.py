"""How many git and jj processes each command starts.

Each costs 5-15 ms -- jj snapshots the working copy on every call -- and a
command starts from a few to over a dozen, so they are most of a local
command's time (`benchmarks/run.py commands` splits it). The counts are
pinned: a change that saves a call lowers its number here, and one that
needs another raises it on purpose.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

typer_testing = pytest.importorskip("typer.testing")

from tether.backends.memory import default_store  # noqa: E402
from tether.cli import app  # noqa: E402

runner = typer_testing.CliRunner()

CALLS = {
    "git": {
        "init": 2,
        "add": 2,
        "status --snapshot": 5,
        "commit": 16,
        "status": 6,
        "new -b": 11,
        "open (forks)": 4,
        "commit on the bookmark": 15,
        "status on the bookmark": 6,
        "verify": 2,
        "ops": 3,
    },
    "jj": {
        "init": 5,
        "add": 3,
        "status --snapshot": 7,
        "commit": 15,
        "status": 8,
        "new -b": 12,
        "open (forks)": 3,
        "commit on the bookmark": 13,
        "status on the bookmark": 8,
        "verify": 3,
        "ops": 4,
    },
}


def test_vcs_processes_per_command(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = "jj" if (vcs_root / ".jj").is_dir() else "git"
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    started: list[list[str]] = []

    class Counting(subprocess.Popen):
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            argv = list(args) if isinstance(args, list | tuple) else [args]
            if os.path.basename(str(argv[0])) in ("git", "jj"):
                started.append([str(a) for a in argv])
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Counting)

    def calls(*args: str) -> int:
        started.clear()
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return len(started)

    counts = {
        "init": calls("init"),
        "add": calls("add", "db", "--kind", "memory", "--set", f"system={system}"),
        "status --snapshot": calls("status", "--snapshot"),
        "commit": calls("commit", "-m", "baseline"),
        "status": calls("status"),
        "new -b": calls("new", "-b", "feat"),
        "open (forks)": calls("open", "db", "--json"),
    }
    fork = next(b for b in store.system(system).branches if b != "main")
    store.write(system, fork, {"rows": 1})
    counts["commit on the bookmark"] = calls("commit", "-m", "rows")
    counts["status on the bookmark"] = calls("status")
    counts["verify"] = calls("verify")
    counts["ops"] = calls("ops")
    assert counts == CALLS[kind]
