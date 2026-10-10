"""Where tether's time goes: startup, commands, a large tree, a long history.

    uv run python benchmarks/run.py                  # everything
    uv run python benchmarks/run.py commands tree    # some of it
    uv run python benchmarks/run.py tree --files 1000000

Everything runs in a temporary directory, removed afterwards (`--keep` keeps
it). `commands` and `history` need `git` and, for their jj half, `jj` on
PATH. Numbers vary with the machine and its load: compare runs on one
machine, with nothing else running.

- **startup**: `python -c "import tether.cli"` against a bare interpreter.
- **commands**: each CLI command in a git and a jj dataset (one memory
  object, one 200-file directory), in-process, split into the time spent
  waiting on git/jj and everything else, with the number of git/jj processes.
- **tree**: the `file` backend's fingerprint of `--files` small files, cold
  (empty hash cache) and warm, each in a fresh process, with peak memory.
- **history**: `plan_gc` over `--commits` commits, which reads every manifest
  in history through one `git cat-file --batch`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

_VCS = ("git", "jj")


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:7.1f} ms"


def _median_wall(argv: list[str], runs: int) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(argv, check=True, capture_output=True)
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def bench_startup(runs: int) -> None:
    bare = _median_wall([sys.executable, "-c", "pass"], runs)
    cli = _median_wall([sys.executable, "-c", "import tether.cli"], runs)
    print(f"startup (median of {runs})")
    print(f"  bare interpreter         {_ms(bare)}")
    print(f"  import tether.cli        {_ms(cli)}")


def _is_vcs(args: Any) -> bool:
    argv = args if isinstance(args, list | tuple) else [args]
    return bool(argv) and os.path.basename(str(argv[0])) in _VCS


class _VcsMeter:
    """Counts git/jj processes, and times the ones `subprocess.run` waits
    for, while `installed()`."""

    def __init__(self) -> None:
        self.calls = 0
        self.seconds = 0.0

    @contextmanager
    def installed(self) -> Iterator[None]:
        self.calls, self.seconds = 0, 0.0
        meter = self
        real_run = subprocess.run

        class Counting(subprocess.Popen):
            def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                if _is_vcs(args):
                    meter.calls += 1
                super().__init__(args, *rest, **kwargs)

        def timed_run(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return real_run(*args, **kwargs)
            finally:
                if _is_vcs(args[0] if args else kwargs.get("args")):
                    meter.seconds += time.perf_counter() - start

        with (
            mock.patch.object(subprocess, "Popen", Counting),
            mock.patch.object(subprocess, "run", timed_run),
        ):
            yield


def _init_vcs(kind: str, root: Path) -> None:
    root.mkdir(parents=True)
    if kind == "jj":
        subprocess.run(["jj", "git", "init"], cwd=root, check=True, capture_output=True)
        return
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for key, value in (("user.email", "bench@tether.dev"), ("user.name", "bench")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)


def _make_files(root: Path, count: int, per_dir: int = 1000) -> None:
    for i in range(count):
        folder = root / f"d{i // per_dir:04d}"
        if i % per_dir == 0:
            folder.mkdir(parents=True, exist_ok=True)
        (folder / f"f{i:07d}.csv").write_bytes(f"row,{i}\n".encode() * 8)


def bench_commands(work: Path, runs: int) -> None:
    from typer.testing import CliRunner

    from tether.backends.memory import default_store
    from tether.cli import app

    runner = CliRunner()
    meter = _VcsMeter()
    data = work / "data"
    _make_files(data, 200, per_dir=200)
    print(f"commands (in-process, so without startup; repeats: median of {runs})")
    print(f"  {'':5} {'command':<20} {'total':>10} {'git/jj':>10} {'calls':>6}")
    here = Path.cwd()
    try:
        for kind in _VCS:
            if shutil.which(kind) is None:
                print(f"  {kind}: not on PATH, skipped")
                continue
            root = work / f"ds-{kind}"
            _init_vcs(kind, root)
            os.chdir(root)
            system = f"bench-{uuid.uuid4().hex[:8]}"
            default_store().system(system)
            steps: list[tuple[str, list[str], int]] = [
                ("init", ["init"], 1),
                (
                    "add (memory)",
                    ["add", "db", "--kind", "memory", "--set", f"system={system}"],
                    1,
                ),
                ("add (file dir)", ["add", "raw", str(data), "--kind", "file"], 1),
                ("status --snapshot", ["status", "--snapshot"], runs),
                ("commit", ["commit", "-m", "baseline"], 1),
                ("status", ["status"], runs),
                ("verify", ["verify"], runs),
                ("new -b feat", ["new", "-b", "feat"], 1),
                ("status (on feat)", ["status"], runs),
                ("diff", ["diff"], runs),
                ("ops", ["ops"], runs),
            ]
            for label, args, repeat in steps:
                totals, waits, calls = [], [], 0
                for _ in range(repeat):
                    with meter.installed():
                        start = time.perf_counter()
                        result = runner.invoke(app, args)
                        totals.append(time.perf_counter() - start)
                    if result.exit_code != 0:
                        raise SystemExit(f"tether {' '.join(args)}: {result.output}")
                    waits.append(meter.seconds)
                    calls = meter.calls
                total, wait = statistics.median(totals), statistics.median(waits)
                print(f"  {kind:5} {label:<20} {_ms(total)} {_ms(wait)} {calls:6d}")
    finally:
        os.chdir(here)


_FINGERPRINT = """
import json, resource, sys, time
from pathlib import Path
from tether.backends.file import FileBackend
backend = FileBackend()
backend.configure_cache(Path(sys.argv[2]))
start = time.perf_counter()
backend.fingerprint({"uri": sys.argv[1]}, None)
seconds = time.perf_counter() - start
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"seconds": seconds, "peak": peak, "hashed": backend._hashes.hashed}))
"""


def bench_tree(work: Path, files: int) -> None:
    from tether.backends.file import _RACY_WINDOW_NS

    tree = work / "tree"
    print(f"tree ({files:,} files of ~80 B; each run in a fresh process)")
    start = time.perf_counter()
    _make_files(tree, files)
    print(f"  made in {time.perf_counter() - start:.1f} s")
    # The cache distrusts a hash taken this soon after the file changed, and
    # the warm run would read every file again.
    time.sleep(_RACY_WINDOW_NS / 1e9 + 0.5)
    cache = work / "tree-cache"
    unit = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes vs KiB
    for label in ("cold", "warm"):
        out = subprocess.run(
            [sys.executable, "-c", _FINGERPRINT, str(tree), str(cache)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(out.stdout)
        peak = result["peak"] * unit / 1e6
        per_file = result["seconds"] / files * 1e6
        print(
            f"  {label}  {result['seconds']:7.2f} s  {per_file:5.1f} us/file  "
            f"peak {peak:6.0f} MB  hashed {result['hashed']:,}"
        )


def bench_history(work: Path, commits: int, kinds: list[str]) -> None:
    from tether.backends.memory import default_store
    from tether.repo import Repo

    print(f"history ({commits} commits of 5 memory objects; median of 3)")
    for kind in kinds:
        if shutil.which(kind) is None:
            print(f"  {kind}: not on PATH, skipped")
            continue
        root = work / f"hist-{kind}"
        _init_vcs(kind, root)
        repo = Repo.init(root)
        store = default_store()
        systems = []
        for n in range(5):
            system = f"hist-{uuid.uuid4().hex[:8]}"
            store.system(system)
            repo.add(f"obj{n}", "memory", {"system": system, "branch": "main"})
            systems.append(system)
        start = time.perf_counter()
        for i in range(commits):
            store.write(systems[i % 5], "main", {"v": i})
            repo.commit(f"c{i}")
        made = time.perf_counter() - start
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            repo.plan_gc()
            timings.append(time.perf_counter() - start)
        print(
            f"  {kind:5} plan_gc {_ms(statistics.median(timings))} "
            f"(history made in {made:.1f} s)"
        )


SECTIONS: dict[str, Callable[[argparse.Namespace, Path], None]] = {
    "startup": lambda a, _work: bench_startup(a.runs),
    "commands": lambda a, work: bench_commands(work, a.runs),
    "tree": lambda a, work: bench_tree(work, a.files),
    "history": lambda a, work: bench_history(work, a.commits, a.vcs.split(",")),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Where tether's time goes.",
        epilog=f"sections: {', '.join(SECTIONS)} (default: all)",
    )
    parser.add_argument("sections", nargs="*", help="sections to run")
    parser.add_argument("--runs", type=int, default=5, help="repeats per timing")
    parser.add_argument("--files", type=int, default=100_000, help="tree size")
    parser.add_argument("--commits", type=int, default=200, help="history length")
    parser.add_argument("--vcs", default="git", help="history: git, jj, or git,jj")
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir")
    args = parser.parse_args()
    unknown = sorted(set(args.sections) - set(SECTIONS))
    if unknown:
        parser.error(f"unknown section(s): {', '.join(unknown)}")
    work = Path(tempfile.mkdtemp(prefix="tether-bench-"))
    # jj keeps each repository's config under the user's config directory,
    # outside the repository; both point into the scratch dir so that nothing
    # a run creates outlives it.
    jj_config = work / "jj.toml"
    jj_config.write_text(
        '[user]\nname = "bench"\nemail = "bench@tether.dev"\n', encoding="utf-8"
    )
    os.environ["JJ_CONFIG"] = str(jj_config)
    os.environ["XDG_CONFIG_HOME"] = str(work / "xdg")
    try:
        for name in args.sections or list(SECTIONS):
            SECTIONS[name](args, work)
            print()
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
