"""The native scanner (`tether_native`) against the Python one it stands in for.

Both run on the same trees -- nested, empty and multi-chunk files; names with
non-ASCII, `#`, spaces and control characters; every kind of symlink; a FIFO;
the owner marker at the root and below it -- and must agree on the state, the
listing, the cache file, the diff of two listings and what they raise. The
differential tests skip where `tether-vcs-native` is not installed
(`uv sync --group native`); the switches that choose a scanner run anyway.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import signal
import sys
import time
import types
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from tether.backends import file as file_backend
from tether.backends.base import MAX_DIFF_ENTRIES
from tether.backends.file import FileBackend, _dump_listing
from tether.errors import BackendError

needs_native = pytest.mark.skipif(
    importlib.util.find_spec("tether_native") is None,
    reason="tether-vcs-native is not installed (uv sync --group native)",
)


@pytest.fixture
def no_racy_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust entries for files written moments ago, so read counts are exact."""
    monkeypatch.setattr(file_backend, "_RACY_WINDOW_NS", 0)


def _backend(
    native: bool, monkeypatch: pytest.MonkeyPatch, cache_dir: Path | None = None
) -> FileBackend:
    monkeypatch.setenv("TETHER_NATIVE", "1" if native else "0")
    backend = FileBackend()
    assert (backend._native is not None) is native
    if cache_dir is not None:
        backend.configure_cache(cache_dir)
    return backend


def _both(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[FileBackend, FileBackend]:
    return (
        _backend(False, monkeypatch, tmp_path / "cache-py"),
        _backend(True, monkeypatch, tmp_path / "cache-native"),
    )


def _try(make: Callable[..., object], *args: object) -> bool:
    """Make one awkward entry, or report that this filesystem refuses it."""
    try:
        make(*args)
    except (OSError, ValueError):
        return False
    return True


_NAMES = [
    "plain.csv",
    "é.txt",
    "日本語.bin",
    "run#1",
    "with space",
    "q?mark",
    "tab\tname",
    "new\nline",
    "back\\slash",
    'quote"d',
    "del\x7f",
    "emoji😀",
    ".hidden",
    ".tether-ownerish",
]


def _varied_tree(root: Path) -> None:
    (root / "sub" / "deep").mkdir(parents=True)
    for i, name in enumerate(_NAMES):
        _try((root / name).write_bytes, f"{i}\n".encode() * (i + 1))
        _try((root / "sub" / name).write_bytes, f"sub {i}\n".encode())
    (root / "empty").write_bytes(b"")
    # Past both scanners' read chunk (1 MiB native, 4 MiB Python).
    (root / "sub" / "deep" / "big.bin").write_bytes(random.Random(0).randbytes(5 << 20))
    (root / ".tether-owner").write_text("22620a34\n", encoding="utf-8")
    (root / "sub" / ".tether-owner").write_text("not the root's\n", encoding="utf-8")
    os.symlink("plain.csv", root / "to-file")
    os.symlink("sub", root / "to-dir")
    os.symlink("nowhere", root / "dangling")
    os.symlink("to-file", root / "chain")
    os.symlink(str(root / "sub" / "é.txt"), root / "absolute")
    os.symlink("../plain.csv", root / "sub" / "up")
    os.symlink("deep/big.bin", root / "sub" / "é-link")
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "fifo")


def _random_tree(root: Path, seed: int) -> None:
    rng = random.Random(seed)
    alphabet = "abcXYZ019 #.-_é日😀"
    dirs = [root]
    for _ in range(rng.randint(5, 40)):
        parent = rng.choice(dirs)
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        name = name.strip(" .") or "x"
        target = parent / name
        if target.exists():
            continue
        roll = rng.random()
        if roll < 0.2:
            target.mkdir()
            dirs.append(target)
        elif roll < 0.3:
            os.symlink(rng.choice(["..", "missing", "../" + name]), target)
        else:
            target.write_bytes(rng.randbytes(rng.choice([0, 1, 100, 5000])))


def _cache_entries(cache_dir: Path) -> dict[str, list]:
    """The cache file, without the time each entry was hashed."""
    entries = json.loads((cache_dir / "file-hashes.json").read_text(encoding="utf-8"))
    return {k: v[:5] for k, v in entries.items()}


@needs_native
def test_a_varied_tree_gets_one_state_listing_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    _varied_tree(root)
    loc = {"uri": str(root)}
    python, native = _both(monkeypatch, tmp_path)
    state = python.fingerprint(loc, None)
    assert native.fingerprint(loc, None) == state
    assert native.listing(loc, state) == python.listing(loc, state)
    assert native._hashes.hashed == python._hashes.hashed
    assert _cache_entries(tmp_path / "cache-native") == _cache_entries(
        tmp_path / "cache-py"
    )
    for name in ("sub/deep/big.bin", "chain", "to-file"):
        single = {"uri": str(root / name)}
        assert native.fingerprint(single, None) == python.fingerprint(single, None)


@needs_native
@pytest.mark.parametrize("seed", range(12))
def test_random_trees_agree(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    _random_tree(root, seed)
    loc = {"uri": str(root)}
    python, native = _both(monkeypatch, tmp_path)
    state = python.fingerprint(loc, None)
    assert native.fingerprint(loc, None) == state
    assert native.listing(loc, state) == python.listing(loc, state)


@needs_native
@pytest.mark.usefixtures("no_racy_window")
def test_each_scanner_reads_the_cache_the_other_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(6):
        (root / f"{i}.bin").write_bytes(bytes([i]) * 64)
    loc, cache = {"uri": str(root)}, tmp_path / "cache"
    first = _backend(False, monkeypatch, cache)
    state = first.fingerprint(loc, None)
    assert first._hashes.hashed == 6
    native = _backend(True, monkeypatch, cache)
    assert native.fingerprint(loc, None) == state and native._hashes.hashed == 0
    (root / "3.bin").write_bytes(b"changed")
    changed = native.fingerprint(loc, None)
    assert changed != state and native._hashes.hashed == 1
    python = _backend(False, monkeypatch, cache)
    assert python.fingerprint(loc, None) == changed and python._hashes.hashed == 0


@needs_native
@pytest.mark.usefixtures("no_racy_window")
@pytest.mark.parametrize("uri", [".", "./", "sub/..", "tree", "./tree/"])
def test_a_relative_root_is_cached_under_the_keys_python_writes(
    uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pathlib drops a root of `.` from `str(root / rel)`.
    work = tmp_path / "work"
    for rel in ("a.bin", "sub/b.bin", "tree/c.bin", "tree/d/e.bin"):
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(rel.encode())
    monkeypatch.chdir(work)
    loc = {"uri": uri}
    python, native = _both(monkeypatch, tmp_path)
    state = python.fingerprint(loc, None)
    assert native.fingerprint(loc, None) == state
    assert _cache_entries(tmp_path / "cache-native") == _cache_entries(
        tmp_path / "cache-py"
    )
    reader = _backend(True, monkeypatch, tmp_path / "cache-py")
    assert reader.fingerprint(loc, None) == state and reader._hashes.hashed == 0


class _UndecodableScanner:
    """A native cache meeting a name it cannot decode, as `tether_native`
    raises ValueError on one that is not UTF-8."""

    hashed = 0

    def fingerprint_dir(
        self, root: str, owner_marker: str, racy_window_ns: int, workers: int
    ) -> NoReturn:
        raise ValueError(f"not UTF-8: {root}")

    def fingerprint_file(self, path: str, racy_window_ns: int) -> NoReturn:
        raise ValueError(f"not UTF-8: {path}")


@pytest.mark.usefixtures("no_racy_window")
def test_a_tree_the_native_scanner_cannot_decode_goes_to_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only some filesystems take a name that is not UTF-8; this reaches the
    # fallback anywhere. It hashes in memory, leaving the shared file alone.
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(3):
        (root / f"{i}.bin").write_bytes(bytes([i]))
    tree, single = {"uri": str(root)}, {"uri": str(root / "0.bin")}
    python = _backend(False, monkeypatch, tmp_path / "cache-py")
    backend = _backend(False, monkeypatch, tmp_path / "cache")
    backend._hashes = _UndecodableScanner()
    state = backend.fingerprint(tree, None)
    assert state == python.fingerprint(tree, None)
    assert backend.fingerprint(single, None) == python.fingerprint(single, None)
    fallback = backend._fallback_hashes
    assert fallback is not None and fallback._path is None and fallback.hashed == 3
    assert backend.fingerprint(tree, None) == state and fallback.hashed == 3


@needs_native
def test_both_read_again_what_was_hashed_too_soon_after_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(3):
        (root / f"{i}.bin").write_bytes(bytes([i]))
    loc = {"uri": str(root)}
    for native in (False, True):
        backend = _backend(native, monkeypatch, tmp_path / f"cache-{native}")
        backend.fingerprint(loc, None)
        backend.fingerprint(loc, None)
        assert backend._hashes.hashed == 6


@needs_native
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_process_forked_after_a_scan_can_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker forked after its parent scanned (multiprocessing, a job
    runner) scans too: no hashing threads outlive a scan for a fork to lose."""
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(20):
        (root / f"{i}.bin").write_bytes(bytes([i]) * 10)
    native = _backend(True, monkeypatch)
    loc = {"uri": str(root)}
    native.fingerprint(loc, None)
    (root / "late.bin").write_bytes(b"x")
    with warnings.catch_warnings():
        # Forking with threads, and the fork hooks of libraries earlier tests
        # imported (lance's warns that it is not fork-safe).
        warnings.simplefilter("ignore")
        pid = os.fork()
    if pid == 0:
        code = 1
        try:
            code = 0 if native.fingerprint(loc, None)["count"] == 21 else 1
        finally:
            os._exit(code)
    deadline = time.monotonic() + 30
    while not (waited := os.waitpid(pid, os.WNOHANG))[0]:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the forked process hung in its scan")
        time.sleep(0.05)
    assert os.waitstatus_to_exitcode(waited[1]) == 0


def _failure(backend: FileBackend, loc: dict) -> str:
    with pytest.raises(BackendError) as err:
        backend.fingerprint(loc, None)
    return str(err.value)


@needs_native
@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs an unprivileged POSIX user"
)
@pytest.mark.parametrize("what", ["file", "directory"])
def test_an_unreadable_entry_fails_both_alike(
    what: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "f.bin").write_bytes(b"x")
    blocked = root / "locked" if what == "directory" else root / "locked" / "f.bin"
    blocked.chmod(0)
    try:
        python, native = _both(monkeypatch, tmp_path)
        message = _failure(python, {"uri": str(root)})
        assert "Permission denied" in message
        assert _failure(native, {"uri": str(root)}) == message
    finally:
        blocked.chmod(0o755)


@needs_native
def test_a_symlink_loop_fails_both_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    os.symlink("b", root / "a")
    os.symlink("a", root / "b")
    python, native = _both(monkeypatch, tmp_path)
    message = _failure(python, {"uri": str(root)})
    assert _failure(native, {"uri": str(root)}) == message


@needs_native
@pytest.mark.skipif(sys.platform == "darwin", reason="macOS names must be UTF-8")
def test_names_that_are_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root that is not UTF-8 holding UTF-8 names fingerprints the same way
    either way; a name below it that is not UTF-8 fails both alike."""
    root = tmp_path / os.fsdecode(b"latin-\xe9")
    if not _try(root.mkdir):
        pytest.skip("this filesystem refuses names that are not UTF-8")
    (root / "ok.txt").write_bytes(b"ok")
    loc = {"uri": str(root)}
    python, native = _both(monkeypatch, tmp_path)
    assert native.fingerprint(loc, None) == python.fingerprint(loc, None)
    (root / os.fsdecode(b"bad-\xff")).write_bytes(b"x")
    errors = []
    for backend in (python, native):
        with pytest.raises(Exception) as err:
            backend.fingerprint(loc, None)
        errors.append(type(err.value))
    assert errors[0] is errors[1]


def _listing(rng: random.Random, paths: list[str]) -> dict[str, tuple[str, int]]:
    return {p: (f"{rng.getrandbits(32):08x}", rng.randint(0, 5000)) for p in paths}


@needs_native
@pytest.mark.parametrize("size", [0, 10, 3 * MAX_DIFF_ENTRIES])
def test_diffs_of_stored_listings_agree(
    size: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(size)
    paths = [f"d{rng.randint(0, 9)}/f{i}é.csv" for i in range(size)]
    a = _listing(rng, paths)
    b = dict(a)
    for path in rng.sample(paths, k=size // 3):
        b[path] = (b[path][0], b[path][1] + rng.randint(-10, 10))
    for path in rng.sample(paths, k=size // 4):
        b.pop(path, None)
    b.update(_listing(rng, [f"new/{i}" for i in range(size // 5)]))
    text_a, text_b = _dump_listing(a), _dump_listing(b) + "\n"
    states = ({"type": "dir", "digest": "a"}, {"type": "dir", "digest": "b"})
    python, native = _both(monkeypatch, tmp_path)
    loc = {"uri": str(tmp_path)}
    expected = python.diff(loc, *states, listings=(text_a, text_b)).to_dict()
    got = native.diff(loc, *states, listings=(text_a, text_b)).to_dict()
    assert got == expected
    assert got["truncated"] is (size == 3 * MAX_DIFF_ENTRIES)


@needs_native
def test_a_listing_only_python_reads_falls_back_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = ({"type": "dir", "digest": "a"}, {"type": "dir", "digest": "b"})
    loc = {"uri": str(tmp_path)}
    numeric = '{"p":"x","k":1,"s":"2"}\n'  # Python's `str()` and `int()` cope
    python, native = _both(monkeypatch, tmp_path)
    assert (
        native.diff(loc, *states, listings=(numeric, "")).to_dict()
        == python.diff(loc, *states, listings=(numeric, "")).to_dict()
    )
    for backend in (python, native):
        with pytest.raises(KeyError):
            backend.diff(loc, *states, listings=('{"k":"x"}\n', ""))


@needs_native
def test_listing_text_escapes_as_json_dumps_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TETHER_NATIVE", raising=False)
    tether_native = file_backend._native_scanner()
    assert tether_native is not None
    rng = random.Random(7)
    pool = [chr(c) for c in range(0x80)] + ["é", "\u2028", "\ufeff", "😀", "\U0001d538"]
    rows = {
        "".join(rng.choice(pool) for _ in range(rng.randint(0, 12))): (
            "".join(rng.choice(pool) for _ in range(4)),
            rng.randint(0, 2**40),
        )
        for _ in range(300)
    }
    assert tether_native.dump_listing(rows) == _dump_listing(rows)


def test_tether_native_0_keeps_the_python_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TETHER_NATIVE", "0")
    assert file_backend._native_scanner() is None
    assert isinstance(FileBackend()._hashes, file_backend._HashCache)


def test_a_native_module_of_another_api_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TETHER_NATIVE", raising=False)
    other = types.ModuleType("tether_native")
    monkeypatch.setitem(sys.modules, "tether_native", other)
    monkeypatch.setattr(
        other, "API_VERSION", file_backend.NATIVE_API + 1, raising=False
    )
    assert file_backend._native_scanner() is None
    monkeypatch.setattr(other, "API_VERSION", file_backend.NATIVE_API)
    assert file_backend._native_scanner() is other
