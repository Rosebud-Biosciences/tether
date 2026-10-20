"""How many git and jj processes each command starts.

Each costs 5-15 ms -- jj snapshots the working copy on every call -- and a
command starts from a few to over a dozen, so they are most of a local
command's time (`benchmarks/run.py commands` splits it). The counts are
pinned: a change that saves a call lowers its number here, and one that
needs another raises it on purpose.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
import uuid
from pathlib import Path
from typing import Any

import pytest

from tether import vcs
from tether.errors import VcsError
from tether.vcs import JjAdapter

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


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The argv of every git and jj process the test starts."""
    argvs: list[list[str]] = []

    class Counting(subprocess.Popen):
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            argv = list(args) if isinstance(args, list | tuple) else [args]
            if os.path.basename(str(argv[0])) in ("git", "jj"):
                argvs.append([str(a) for a in argv])
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return argvs


def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget what this process learned of jj, as a new `tether` would."""
    monkeypatch.setattr(vcs, "_jj_checked", set())
    monkeypatch.setattr(vcs, "_jj_configs", {})


def test_vcs_processes_per_command(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch, started: list[list[str]]
) -> None:
    kind = "jj" if (vcs_root / ".jj").is_dir() else "git"
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)

    def calls(*args: str) -> int:
        _fresh_process(monkeypatch)
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


def _asked(argv: list[str]) -> str:
    if argv[1:] == ["--version"]:
        return "version"
    if "config" in argv and argv[argv.index("config") + 1 :][:1] == ["list"]:
        return "config list"
    return "call"


def test_adapters_in_one_process_share_the_version_check_and_config_listing(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """Every `Repo` makes its own adapter. The second on a root starts only
    its own call; one on another root lists the config again, for that
    root's scopes and repo config, but takes the version check as made."""
    if not (vcs_root / ".jj").is_dir():
        pytest.skip("jj only")
    _fresh_process(monkeypatch)
    other = tmp_path_factory.mktemp("other")
    subprocess.run(["jj", "git", "init"], cwd=other, check=True, capture_output=True)

    def asked(root: Path) -> list[str]:
        started.clear()
        JjAdapter(root, "jj").current_bookmarks()
        return [_asked(argv) for argv in started]

    assert asked(vcs_root) == ["version", "config list", "call"]
    assert asked(vcs_root) == ["call"]
    assert asked(other) == ["config list", "call"]
    assert asked(other) == ["call"]
    assert asked(vcs_root) == ["call"]


def test_an_edited_config_is_listed_again(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The user's config file edited (to the same size, which a coarse mtime
    would miss) or a repo-level setting made between two adapters: the
    second lists the config again and goes by the new one. Unchanged, the
    cached answer is what a fresh listing gives."""
    if not (vcs_root / ".jj").is_dir():
        pytest.skip("jj only")
    _fresh_process(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    cfg = tmp_path_factory.mktemp("jjuser") / "config.toml"
    monkeypatch.setenv("JJ_CONFIG", str(cfg))

    def listed() -> tuple[int, dict, str | None]:
        started.clear()
        layer, theirs = vcs._jj_user_config("jj", vcs_root)
        text = Path(layer).read_text(encoding="utf-8")
        return len(started), tomllib.loads(text), theirs

    cfg.write_text('[user]\nname = "Ada Lovelace"\n', encoding="utf-8")
    assert listed() == (1, {"user": {"name": "Ada Lovelace"}}, "all()")
    assert listed()[0] == 0
    assert vcs._jj_user_config("jj", vcs_root) == vcs._list_jj_user_config(
        "jj", vcs_root
    )
    cfg.write_text('[user]\nname = "Grace Hopper"\n', encoding="utf-8")
    assert listed() == (1, {"user": {"name": "Grace Hopper"}}, "all()")
    subprocess.run(
        ["jj", "config", "set", "--repo", "snapshot.auto-track", "none()"],
        cwd=vcs_root,
        check=True,
        capture_output=True,
    )
    assert listed() == (1, {"user": {"name": "Grace Hopper"}}, "none()")
    assert listed()[0] == 0


def test_a_replaced_jj_is_checked_again(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The version check holds for the file it ran: replaced at the same
    path, the binary is asked again, and an older one refused."""
    real = shutil.which("jj")
    if not (vcs_root / ".jj").is_dir() or real is None:
        pytest.skip("jj only")
    _fresh_process(monkeypatch)
    exe = tmp_path_factory.mktemp("bin") / "jj"
    exe.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)

    def versions() -> int:
        started.clear()
        JjAdapter(vcs_root, str(exe)).current_bookmarks()
        return [_asked(argv) for argv in started].count("version")

    assert versions() == 1
    assert versions() == 0
    exe.write_text('#!/bin/sh\necho "jj 0.30.0"\n', encoding="utf-8")
    with pytest.raises(VcsError, match=r"jj 0\.30\.0 is too old"):
        JjAdapter(vcs_root, str(exe)).current_bookmarks()
