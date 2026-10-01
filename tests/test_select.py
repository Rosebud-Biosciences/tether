"""Selecting objects by key or prefix: one helper, every command that takes keys."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from tether.backends.memory import MemoryBackend, default_store
from tether.errors import ConfigError, StalePlanError, VcsError
from tether.handles import MemoryHandle
from tether.manifest import (
    compute_pin_id,
    key_to_relpath,
    listings_dir,
    object_path,
    read_workspace,
    write_object,
)
from tether.plan import Plan
from tether.repo import Repo

typer_testing = pytest.importorskip("typer.testing")

from tether.cli import app  # noqa: E402

runner = typer_testing.CliRunner()

KEYS = ["db", "raw/plate1", "zarr/deep/x", "zarr/imaging", "zarr/labels", "zarrish"]


def _mem(repo: Repo, key: str, system: str | None = None) -> str:
    name = system or f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(name)
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def _spy(
    monkeypatch: pytest.MonkeyPatch, target: Any, method: str, *, bound: bool = True
) -> list[str]:
    """Record the memory system each call of `target.method` reads. `bound`
    patches an instance (the Repo's cached backend); otherwise the class, for
    the CLI's own Repo."""
    calls: list[str] = []
    real = getattr(target, method)

    if bound:

        def spy(locator: dict, *args: Any, **kwargs: Any) -> Any:
            calls.append(str(locator["system"]))
            return real(locator, *args, **kwargs)
    else:

        def spy(self: Any, locator: dict, *args: Any, **kwargs: Any) -> Any:
            calls.append(str(locator["system"]))
            return real(self, locator, *args, **kwargs)

    monkeypatch.setattr(target, method, spy)
    return calls


@pytest.fixture
def repo(vcs_root: Path) -> Repo:
    repo = Repo.init(vcs_root)
    for key in KEYS:
        _mem(repo, key)
    return repo


def _systems(repo: Repo, keys: list[str]) -> list[str]:
    return sorted(str(repo.objects[k].locator["system"]) for k in keys)


def _write(repo: Repo, key: str, payload: dict) -> None:
    handle = repo.open(key)
    assert isinstance(handle, MemoryHandle)
    handle.write(payload)


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        (None, KEYS),
        ([], KEYS),
        (["db"], ["db"]),
        (["zarr/imaging"], ["zarr/imaging"]),
        # A prefix is a path segment boundary: `zarrish` is not under `zarr/`.
        (["zarr/"], ["zarr/deep/x", "zarr/imaging", "zarr/labels"]),
        (["zarr/deep/"], ["zarr/deep/x"]),
        # Overlapping and repeated selectors name each key once, sorted.
        (["zarr/", "zarr/imaging"], ["zarr/deep/x", "zarr/imaging", "zarr/labels"]),
        (["zarr/imaging", "zarr/deep/", "zarr/"], KEYS[2:5]),
        (["db", "db", "raw/"], ["db", "raw/plate1"]),
        (["zarrish", "db"], ["db", "zarrish"]),
    ],
)
def test_select_keys_takes_exact_keys_and_prefixes(
    repo: Repo, selectors: list[str] | None, expected: list[str]
) -> None:
    assert repo.select_keys(selectors) == expected


@pytest.mark.parametrize(
    ("selectors", "named"),
    [
        (["nope"], ["no such object: nope"]),
        # Without its `/` a prefix is a key, and `zarr` is none.
        (["zarr"], ["no such object: zarr"]),
        (["nope/"], ["no object under nope/"]),
        (["db/"], ["no object under db/"]),
        (
            ["db", "nope", "zarr/", "gone/", "other"],
            ["no such object: nope, other", "no object under gone/"],
        ),
    ],
)
def test_a_selector_that_matches_nothing_is_named(
    repo: Repo, selectors: list[str], named: list[str]
) -> None:
    with pytest.raises(ConfigError) as err:
        repo.select_keys(selectors)
    for text in named:
        assert text in str(err.value)


def test_select_keys_selects_among_other_key_sets(repo: Repo) -> None:
    among = ["old/a", "old/b", "db"]
    assert repo.select_keys(["old/"], among=among) == ["old/a", "old/b"]
    assert repo.select_keys(None, among=among) == ["db", "old/a", "old/b"]
    with pytest.raises(ConfigError, match="no such object: zarr/imaging"):
        repo.select_keys(["zarr/imaging"], among=among)


# --------------------------------------------------------------------------- #
# Read-only commands
# --------------------------------------------------------------------------- #
def test_status_and_snapshot_contact_only_the_selection(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("baseline")
    store = default_store()
    for key in KEYS:
        store.write(str(repo.objects[key].locator["system"]), "main", {"k": key})
    backend = repo.backend_for("memory")
    before = dict(repo.workspace.last_snapshot)
    calls = _spy(monkeypatch, backend, "fingerprint")

    report = repo.status(["zarr/", "db"])
    assert [o.key for o in report.objects] == ["db", *KEYS[2:5]]
    assert all(o.state_label == "modified" for o in report.objects)
    assert sorted(calls) == _systems(repo, ["db", *KEYS[2:5]])
    # What was not selected keeps what the last snapshot cached for it.
    for key in ("raw/plate1", "zarrish"):
        assert repo.workspace.last_snapshot[key] == before[key]
    for key in ("db", "zarr/imaging"):
        assert repo.workspace.last_snapshot[key] != before[key]

    calls.clear()
    states = repo.snapshot(["raw/"])
    assert list(states) == ["raw/plate1"] and calls == _systems(repo, ["raw/plate1"])
    assert repo.workspace.last_snapshot["zarrish"] == before["zarrish"]

    # A cached status of a selection reads nothing.
    calls.clear()
    cached = repo.status(["zarrish"], do_snapshot=False)
    assert not cached.fresh and [o.key for o in cached.objects] == ["zarrish"]
    assert cached.objects[0].state_label == "clean" and calls == []

    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.status(["nope"])
    with pytest.raises(ConfigError, match="no object under nope/"):
        repo.snapshot(["nope/"])
    assert calls == []


def test_a_selective_snapshot_keeps_the_other_objects_cached_errors(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("baseline")
    backend = repo.backend_for("memory")
    broken = str(repo.objects["zarrish"].locator["system"])
    real = backend.fingerprint

    def flaky(locator: dict, ref: str | None) -> dict:
        if locator["system"] == broken:
            raise ConfigError("store unreachable")
        return real(locator, ref)

    monkeypatch.setattr(backend, "fingerprint", flaky)
    failures: dict[str, Exception] = {}
    repo.snapshot(failures=failures)
    assert list(failures) == ["zarrish"]
    repo.snapshot(["db"])
    assert "zarrish" in repo.workspace.last_snapshot_errors
    # And a status of the selection is not an error because of it.
    assert all(o.error is None for o in repo.status(["db"], do_snapshot=False).objects)
    assert repo.status(["zarrish"], do_snapshot=False).objects[0].error


def test_status_reports_staleness_only_for_the_selection(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    systems = {key: _mem(repo, key) for key in ("a", "b")}
    repo.commit("baseline")
    repo.new(bookmark="work")
    backend = repo.backend_for("memory")
    for key in ("a", "b"):  # another workspace's commit, underneath us
        m = repo.objects[key]
        state = {"snapshot_id": default_store().write(systems[key], "main", {"t": 1})}
        pin_id = compute_pin_id(
            m.kind, backend.identity(m.locator), state, repo.config.dataset_id
        )
        pin = backend.pin(m.locator, state, pin_id)
        write_object(repo.root, m.with_pin(state=state, pin=pin))
    reloaded = Repo.find(vcs_root)
    assert reloaded.status(do_snapshot=False).stale_keys == ["a", "b"]
    only_b = reloaded.status(["b"], do_snapshot=False)
    assert only_b.stale and only_b.stale_keys == ["b"]


def test_verify_checks_only_the_selection_at_any_revision(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repo.commit("baseline").vcs_commit
    assert first is not None
    store = default_store()
    store.write(str(repo.objects["db"].locator["system"]), "main", {"v": 2})
    repo.commit("db moved")
    backend = repo.backend_for("memory")
    calls = _spy(monkeypatch, backend, "verify")

    reports = repo.verify(["zarr/"])
    assert list(reports) == KEYS[2:5] and all(r.ok for r in reports.values())
    assert sorted(calls) == _systems(repo, KEYS[2:5])

    calls.clear()
    assert list(repo.verify(["db"], rev=first)) == ["db"] and len(calls) == 1

    # Across history, a key is selected from every key history has had: a
    # removed object is still there to verify at the commits that name it.
    raw = _systems(repo, ["raw/plate1"])
    repo.remove("raw/plate1")
    with pytest.raises(ConfigError, match="no such object: raw/plate1"):
        repo.verify(["raw/plate1"])
    calls.clear()
    history = repo.verify(["raw/"], all_history=True)
    assert history and all(label.endswith(":raw/plate1") for label in history)
    assert calls == raw  # one pin at every commit: verified once
    history = repo.verify(["db", "zarrish"], all_history=True)
    assert {label.split(":", 1)[1] for label in history} == {"db", "zarrish"}
    with pytest.raises(ConfigError, match="no object under nope/"):
        repo.verify(["nope/"], all_history=True)


def test_diff_selects_from_both_sides_and_diffs_only_the_selection(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repo.commit("baseline").vcs_commit
    store = default_store()
    for key in ("db", "zarr/imaging", "zarr/labels"):
        store.write(str(repo.objects[key].locator["system"]), "main", {"k": key})
    repo.remove("raw/plate1")
    second = repo.commit("three moved, one removed").vcs_commit
    assert first is not None and second is not None
    backend = repo.backend_for("memory")
    calls = _spy(monkeypatch, backend, "diff")

    entries = repo.diff(first, second, keys=["zarr/"], content=True)
    assert [(e.key, e.change) for e in entries] == [
        ("zarr/deep/x", "unchanged"),
        ("zarr/imaging", "changed"),
        ("zarr/labels", "changed"),
    ]
    assert sorted(calls) == _systems(repo, ["zarr/imaging", "zarr/labels"])
    # A key only one side has is selectable, from either direction.
    (gone,) = repo.diff(first, second, keys=["raw/plate1"])
    assert gone.change == "removed"
    (back,) = repo.diff(second, first, keys=["raw/"])
    assert back.change == "added"
    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.diff(first, second, keys=["db", "nope"])
    # The working tree against its parent, selected the same way.
    assert [e.key for e in repo.diff(keys=["db"])] == ["db"]


def test_restore_and_promote_select_the_same_way(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    for key in ("a", "c/x", "c/y"):
        _mem(repo, key)
    first = repo.commit("baseline").vcs_commit
    assert first is not None
    repo.new(bookmark="work", eager=True)
    for key in ("c/x", "c/y"):
        _write(repo, key, {"k": key})
    repo.commit("wrote c")

    plan = repo.plan_restore(["c/", "c/x"], first, discard=True)
    assert sorted(a.key for a in plan.actions if a.op == "fork") == ["c/x", "c/y"]
    with pytest.raises(ConfigError, match="no object under d/"):
        repo.plan_restore(["d/"], first)
    # Restore resets branches: a selection that came out empty is refused, and
    # only `None` asks for every object.
    for empty in ([], ()):
        with pytest.raises(ConfigError, match="restore needs at least one key"):
            repo.plan_restore(empty, first)
        with pytest.raises(ConfigError, match="restore needs at least one key"):
            repo.restore(empty, first)
    plan = repo.plan_restore(None, first, discard=True)
    assert sorted(a.key for a in plan.actions if a.op == "fork") == ["a", "c/x", "c/y"]

    plan = repo.plan_promote(["c/"])
    assert sorted(a.key for a in plan.writes) == ["c/x", "c/y"]
    assert plan.context["subset"] is True
    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.plan_promote(["c/", "nope"])
    # At a revision, selected among that revision's manifests.
    repo.remove("a")
    plan = repo.plan_promote(["a"], rev=first)
    assert plan.notes == ["a: base already at the target"]
    with pytest.raises(ConfigError, match="no such object: a"):
        repo.plan_promote(["a"])


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #
def test_cli_read_only_commands_take_selectors(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo.root)
    first = repo.commit("baseline").vcs_commit
    assert first is not None
    store = default_store()
    for key in ("db", "zarr/imaging"):
        store.write(str(repo.objects[key].locator["system"]), "main", {"k": key})
    calls = _spy(monkeypatch, MemoryBackend, "fingerprint", bound=False)

    r = runner.invoke(app, ["status", "zarr/", "db", "--snapshot", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert [o["key"] for o in payload["objects"]] == ["db", *KEYS[2:5]]
    assert {o["key"]: o["state"] for o in payload["objects"]}["db"] == "modified"
    assert sorted(calls) == _systems(repo, ["db", *KEYS[2:5]])

    calls.clear()
    r = runner.invoke(app, ["snapshot", "zarr/imaging", "raw/", "--json"])
    assert r.exit_code == 0, r.output
    assert sorted(json.loads(r.output)) == ["raw/plate1", "zarr/imaging"]
    assert sorted(calls) == _systems(repo, ["raw/plate1", "zarr/imaging"])

    second = repo.commit("moved").vcs_commit
    assert second is not None
    r = runner.invoke(
        app, ["diff", first, second, "--key", "zarr/", "--key", "db", "--json"]
    )
    assert r.exit_code == 0, r.output
    changes = {e["key"]: e["change"] for e in json.loads(r.output)}
    assert changes == {
        "db": "changed",
        "zarr/deep/x": "unchanged",
        "zarr/imaging": "changed",
        "zarr/labels": "unchanged",
    }

    r = runner.invoke(app, ["verify", "db", "zarr/deep/", "--json"])
    assert r.exit_code == 0, r.output
    assert sorted(json.loads(r.output)) == ["db", "zarr/deep/x"]

    for argv in (
        ["status", "nope"],
        ["snapshot", "nope/"],
        ["verify", "nope"],
        ["diff", "--key", "nope"],
    ):
        r = runner.invoke(app, argv)
        assert r.exit_code == 1 and "nope" in r.output, (argv, r.output)


def test_cli_exit_codes_follow_the_selection(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` and `verify` exit 1 for a failure among what they report,
    and 0 when the failing object is not selected."""
    monkeypatch.chdir(repo.root)
    repo.commit("baseline")
    lost = repo.objects["zarrish"]
    assert lost.pin is not None
    repo.backend_for("memory").unpin(dict(lost.locator), lost.pin)
    r = runner.invoke(app, ["verify", "--json"])
    assert r.exit_code == 1 and "zarrish" in json.loads(r.output)
    r = runner.invoke(app, ["verify", "zarr/", "db", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert sorted(payload) == ["db", *KEYS[2:5]]
    assert {v["status"] for v in payload.values()} == {"ok"}

    broken = str(lost.locator["system"])
    real = MemoryBackend.fingerprint

    def flaky(self: MemoryBackend, locator: dict, ref: str | None) -> dict:
        if locator["system"] == broken:
            raise ConfigError("store unreachable")
        return real(self, locator, ref)

    monkeypatch.setattr(MemoryBackend, "fingerprint", flaky)
    r = runner.invoke(app, ["status", "zarrish", "--snapshot", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["objects"][0]["state"] == "error"
    r = runner.invoke(app, ["status", "db", "--snapshot", "--json"])
    assert r.exit_code == 0, r.output
    assert [o["key"] for o in json.loads(r.output)["objects"]] == ["db"]


# --------------------------------------------------------------------------- #
# commit KEY... and pull --key
# --------------------------------------------------------------------------- #
ODD = "odd dir/a (1) [x]"
"""A key whose manifest path is fileset and pathspec syntax to jj and git."""


def _rel(repo: Repo, key: str) -> str:
    return (repo._dataset_rel() / ".tether" / key_to_relpath(key)).as_posix()


def _text(repo: Repo, key: str) -> str:
    return object_path(repo.root, key).read_text(encoding="utf-8")


def _at(repo: Repo, commit: str | None, key: str) -> str | None:
    assert commit is not None
    return repo.vcs.read_file_at(commit, _rel(repo, key))


def _dirty(repo: Repo, *keys: str) -> list[str]:
    return [k for k in keys if repo.vcs.dirty([_rel(repo, k)])]


@pytest.fixture
def moved(vcs_root: Path) -> Repo:
    """On the trunk: `a`, `b` and `ODD` committed, then moved upstream (their
    position); `c` added and not committed; `tether.toml` edited."""
    repo = Repo.init(vcs_root)
    for key in ("a", "b", ODD):
        _mem(repo, key)
    repo.commit("baseline")
    for key in ("a", "b", ODD):
        default_store().write(_systems(repo, [key])[0], "main", {"k": key})
    _mem(repo, "c")
    with (repo.root / "tether.toml").open("a", encoding="utf-8") as fh:
        fh.write("# a local note\n")
    return repo


def test_commit_keys_commits_only_the_selection(
    moved: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = moved
    baseline = {k: _text(repo, k) for k in ("a", "b", ODD)}
    ws = read_workspace(repo.root)
    calls = _spy(monkeypatch, repo.backend_for("memory"), "fingerprint")

    result = repo.commit("a and odd", keys=["a", "odd dir/"])
    assert sorted(result.pinned) == ["a", ODD]
    assert sorted(calls) == _systems(repo, ["a", ODD])
    commit = result.vcs_commit
    for key in ("a", ODD):
        assert _at(repo, commit, key) == _text(repo, key) != baseline[key]
    # Everything else is as last committed, in the commit and on disk.
    assert _at(repo, commit, "b") == _text(repo, "b") == baseline["b"]
    assert _at(repo, commit, "c") is None
    toml = (repo._dataset_rel() / "tether.toml").as_posix()
    assert "# a local note" not in (repo.vcs.read_file_at(str(commit), toml) or "")
    assert _dirty(repo, "a", "b", ODD, "c") == ["c"]
    assert repo.vcs.dirty([toml])
    after = read_workspace(repo.root)
    assert after.last_snapshot["b"] == ws.last_snapshot["b"]
    assert after.last_snapshot["a"] != ws.last_snapshot["a"]
    (b,) = repo.status(["b"]).objects
    assert b.state_label == "modified"

    # A later full commit picks up the rest: b's move, c's add, the edit.
    rest = repo.commit("the rest")
    assert sorted(rest.pinned) == ["b", "c"] and sorted(rest.unchanged) == ["a", ODD]
    assert _at(repo, rest.vcs_commit, "c") == _text(repo, "c")
    assert not repo.vcs.dirty(repo._vcs_paths())


def test_commit_keys_hands_the_vcs_literal_paths(vcs_root: Path) -> None:
    """A key's `[ab]` is characters, not a glob: the manifests it would match
    as one stay out of the commit."""
    repo = Repo.init(vcs_root)
    for key in ("g/[ab]", "g/a", "g/b"):
        _mem(repo, key)
    repo.commit("baseline")
    default_store().write(_systems(repo, ["g/[ab]"])[0], "main", {"v": 1})
    repo.set_policy(["g/a", "g/b"], pin="record")
    assert _dirty(repo, "g/[ab]", "g/a", "g/b") == ["g/a", "g/b"]

    result = repo.commit("one", keys=["g/[ab]"])
    assert list(result.pinned) == ["g/[ab]"]
    assert _dirty(repo, "g/[ab]", "g/a", "g/b") == ["g/a", "g/b"]
    assert _at(repo, result.vcs_commit, "g/[ab]") == _text(repo, "g/[ab]")


def test_commit_keys_writes_and_commits_only_the_selections_listings(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo = Repo.init(vcs_root)
    for key in ("d1", "d2"):
        data = tmp_path_factory.mktemp(key)
        (data / "x.bin").write_bytes(key.encode())
        repo.add(key, "file", {"uri": str(data)})
    reldir = f"{repo._objects_reldir().rsplit('/', 1)[0]}/listings"

    first = repo.commit("d1", keys=["d1"])
    (listing,) = sorted(listings_dir(repo.root).glob("*.jsonl"))
    assert repo.vcs.list_files_at(str(first.vcs_commit), reldir) == [
        f"{reldir}/{listing.name}"
    ]
    assert _at(repo, first.vcs_commit, "d2") is None and _dirty(repo, "d2") == ["d2"]

    second = repo.commit("d2", keys=["d2"])
    assert len(repo.vcs.list_files_at(str(second.vcs_commit), reldir)) == 2
    assert not repo.vcs.dirty(repo._vcs_paths(["d1", "d2"]))


def test_commit_keys_on_a_bookmark_leaves_the_others_workspace_entries(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    for key in ("a", "b"):
        _mem(repo, key)
    repo.commit("baseline")
    repo.new(bookmark="work")
    _write(repo, "a", {"a": 1})
    _write(repo, "b", {"b": 1})
    ws = read_workspace(repo.root)

    repo.commit("a", keys=["a"])
    after = read_workspace(repo.root)
    for table in ("base_states", "last_snapshot", "working_refs", "fork_points"):
        assert getattr(after, table).get("b") == getattr(ws, table).get("b"), table
    assert after.base_states["a"] != ws.base_states["a"]
    assert not repo.stale_keys()
    labels = {o.key: o.state_label for o in repo.status().objects}
    assert labels == {"a": "clean", "b": "modified"}


def test_undo_of_a_selective_commit_gives_back_only_its_manifests(
    moved: Repo,
) -> None:
    repo = moved
    baseline_odd = _text(repo, ODD)
    repo.commit("a and odd", keys=["a", ODD])
    written = {k: _text(repo, k) for k in ("a", ODD)}

    report = repo.undo()
    assert report.op.command == "commit"
    assert any(line.startswith("uncommitted ") for line in report.restored)
    # Its manifests are working-tree changes again (pins kept); the rest is
    # as it was: b unchanged, c's add still uncommitted.
    assert {k: _text(repo, k) for k in written} == written
    assert _dirty(repo, "a", "b", ODD, "c") == ["a", ODD, "c"]

    # Committing one of them again leaves the other a working-tree change.
    again = repo.commit("a again", keys=["a"])
    assert not again.pinned and again.unchanged == ["a"]
    assert _at(repo, again.vcs_commit, "a") == written["a"]
    assert _at(repo, again.vcs_commit, ODD) == baseline_odd
    assert _dirty(repo, "a", "b", ODD, "c") == [ODD, "c"]


def test_undo_of_a_selective_commit_without_vcs_restores_only_its_manifests(
    moved: Repo,
) -> None:
    repo = moved
    before = {k: _text(repo, k) for k in ("a", "b", ODD, "c")}
    repo.commit("a only", keys=["a"], vcs=False)
    assert [k for k in before if _text(repo, k) != before[k]] == ["a"]
    report = repo.undo()
    manifests = [line for line in report.restored if "manifest" in line]
    assert manifests == ["a: manifest restored"]
    assert {k: _text(repo, k) for k in before} == before


def test_a_saved_selective_plan_applies_exactly_its_selection(moved: Repo) -> None:
    repo = moved
    baseline_b = _text(repo, "b")
    assert "keys" not in repo.plan_commit("all").context
    plan = repo.plan_commit("sel", keys=["odd dir/", "a"])
    assert plan.context["keys"] == ["a", ODD]
    assert sorted(a.key for a in plan.actions if a.op == "pin") == ["a", ODD]
    assert sorted(plan.context["states"]) == ["a", ODD]
    saved = plan.to_json()

    # The selection is part of what the digest vouches for.
    for edit in (["a", "b"], ["a"], None):
        data = json.loads(saved)
        if edit is None:
            del data["context"]["keys"]
        else:
            data["context"]["keys"] = edit
        with pytest.raises(StalePlanError, match="edited after it was saved"):
            repo.apply_commit(Plan.from_json(json.dumps(data)))
    # Keys named at apply must be the plan's.
    with pytest.raises(ConfigError, match=re.escape(f"commits a, {ODD}, not b")):
        repo.apply_commit(Plan.from_json(saved), keys=["b"])
    with pytest.raises(ConfigError, match="commits every object, not a"):
        repo.apply_commit(repo.plan_commit("all"), keys=["a"])

    result = repo.apply_commit(Plan.from_json(saved), keys=["a", "odd dir/"])
    assert sorted(result.pinned) == ["a", ODD]
    assert _at(repo, result.vcs_commit, "b") == baseline_b
    assert _dirty(repo, "b", "c") == ["c"]


def test_a_selective_plan_is_stale_once_any_manifest_changes(moved: Repo) -> None:
    """The plan binds to every manifest, not just the selection's: an object
    registered since may share a selected one's branch space."""
    repo = moved
    plan = repo.plan_commit("a", keys=["a"])
    _mem(repo, "late")
    with pytest.raises(StalePlanError, match="manifests changed"):
        repo.apply_commit(plan)


def test_the_post_commit_check_covers_exactly_the_committed_manifests(
    moved: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = moved
    commit = repo.commit("a", keys=["a"]).vcs_commit
    assert commit is not None
    repo._require_committed(commit, ["a"])
    # The whole working tree's check would trip over c, which it leaves out.
    with pytest.raises(VcsError, match=r"1 of the dataset's manifest\(s\) \(c\)"):
        repo._require_committed(commit)
    # A VCS that dropped the selected manifest is caught.
    real = repo.vcs.files_at
    monkeypatch.setattr(
        repo.vcs,
        "files_at",
        lambda rev, reldir: {
            p: t for p, t in real(rev, reldir).items() if not p.endswith("/a.toml")
        },
    )
    with pytest.raises(VcsError, match=r"\(a\)"):
        repo._require_committed(commit, ["a"])


def test_pull_key_on_the_trunk_pulls_only_the_selection(
    moved: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = moved
    baseline_b = _text(repo, "b")
    # The uncommitted add and edit refuse a whole pull, not a selective one.
    with pytest.raises(ConfigError, match="uncommitted edits"):
        repo.pull()
    ws = read_workspace(repo.root)
    calls = _spy(monkeypatch, repo.backend_for("memory"), "fingerprint")

    report = repo.pull(keys=["a"])
    assert list(report.committed) == ["a"]
    assert not report.unchanged and not report.skipped
    assert calls == _systems(repo, ["a"])
    assert _at(repo, report.vcs_commit, "a") == _text(repo, "a")
    assert _at(repo, report.vcs_commit, "b") == _text(repo, "b") == baseline_b
    assert _at(repo, report.vcs_commit, "c") is None and _dirty(repo, "c") == ["c"]
    assert read_workspace(repo.root).last_snapshot["b"] == ws.last_snapshot["b"]

    again = repo.pull(keys=["a"])
    assert again.unchanged == ["a"] and again.vcs_commit is None
    # The selection's own uncommitted manifest still refuses it.
    with pytest.raises(ConfigError, match="uncommitted edits"):
        repo.pull(keys=["c"])
    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.pull(keys=["nope"])

    monkeypatch.chdir(repo.root)
    r = runner.invoke(app, ["pull", "--key", "odd dir/", "--key", "b", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert sorted(payload["committed"]) == ["b", ODD] and payload["vcs_commit"]
    assert _dirty(repo, "a", "b", ODD, "c") == ["c"]


def test_objects_sharing_a_branch_space_are_committed_and_pulled_together(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    shared = _mem(repo, "s/x")
    _mem(repo, "s/y", system=shared)
    _mem(repo, "t/z", system=shared)
    _mem(repo, "a")
    backend = repo.backend_for("memory")
    scopes = {k: backend.branch_scope(m.locator) for k, m in repo.objects.items()}
    assert scopes["s/x"] == scopes["s/y"] == scopes["t/z"] != scopes["a"]
    repo.commit("baseline")
    store = default_store()
    store.write(shared, "main", {"v": 1})
    store.write(_systems(repo, ["a"])[0], "main", {"v": 1})

    for keys, named, missing in (
        (["s/x"], "s/x shares", "s/y, t/z"),
        (["s/"], "s/x, s/y share", "t/z"),
        (["s/x", "a"], "s/x shares", "s/y, t/z"),
    ):
        with pytest.raises(ConfigError) as err:
            repo.commit("part", keys=keys)
        message = str(err.value)
        assert f"{named} a native branch space with {missing}:" in message
        assert "add " in message and "to the selection" in message
    with pytest.raises(
        ConfigError, match="`tether pull --key s/x --key s/y --key t/z`"
    ):
        repo.pull(keys=["s/x"])
    with pytest.raises(ConfigError, match="t/z"):
        repo.plan_commit("part", keys=["s/"])
    # Read-only commands need no such rule.
    assert [o.key for o in repo.status(["s/x"]).objects] == ["s/x"]
    assert list(repo.verify(["s/y"])) == ["s/y"]

    # Named together, they share one pin; an object alone in its space
    # commits alone.
    together = repo.commit("s", keys=["s/", "t/z"])
    assert sorted(together.pinned) == ["s/x", "s/y", "t/z"]
    assert len({p.id for p in together.pinned.values() if p}) == 1
    assert list(repo.commit("a", keys=["a"]).pinned) == ["a"]

    monkeypatch.chdir(repo.root)
    store.write(shared, "main", {"v": 2})
    r = runner.invoke(app, ["commit", "s/x", "t/z", "-m", "part"])
    assert r.exit_code == 1 and "with s/y" in r.output, r.output
    r = runner.invoke(app, ["pull", "--key", "t/z"])
    assert r.exit_code == 1 and "--key s/x --key s/y --key t/z" in r.output, r.output


def test_cli_commit_takes_selectors_and_plans_keep_them(
    moved: Repo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    repo = moved
    monkeypatch.chdir(repo.root)
    saved = tmp_path_factory.mktemp("plans") / "commit.json"

    r = runner.invoke(app, ["commit", "b", "-m", "b", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["context"]["keys"] == ["b"]
    r = runner.invoke(app, ["commit", "a", ODD, "-m", "sel", "--plan", str(saved)])
    assert r.exit_code == 0, r.output
    assert json.loads(saved.read_text())["context"]["keys"] == ["a", ODD]
    r = runner.invoke(app, ["commit", "b", "--from-plan", str(saved)])
    assert r.exit_code == 1 and "not b" in r.output, r.output
    r = runner.invoke(app, ["commit", "--from-plan", str(saved), "--json"])
    assert r.exit_code == 0, r.output
    assert sorted(json.loads(r.output)["pinned"]) == ["a", ODD]
    assert _dirty(repo, "a", "b", ODD, "c") == ["c"]
    baseline_b = _text(repo, "b")

    r = runner.invoke(app, ["commit", "b", "-m", "b", "--json"])
    assert r.exit_code == 0, r.output
    assert list(json.loads(r.output)["pinned"]) == ["b"]
    assert _text(repo, "b") != baseline_b and _dirty(repo, "a", "b", ODD, "c") == ["c"]
    r = runner.invoke(app, ["commit", "nope/", "-m", "x"])
    assert r.exit_code == 1 and "no object under nope/" in r.output, r.output


# --------------------------------------------------------------------------- #
# repair KEY...
# --------------------------------------------------------------------------- #
def _lose_pins(repo: Repo, keys: list[str]) -> dict[str, str]:
    """Delete each object's pin from its store, as by hand; key -> pin id."""
    lost = {}
    for key in keys:
        m = repo.objects[key]
        assert m.pin is not None
        del default_store().system(str(m.locator["system"])).tags[m.pin.ref]
        lost[key] = m.pin.id
    return lost


def _pinned(repo: Repo) -> list[str]:
    tags = {k: default_store().system(_systems(repo, [k])[0]).tags for k in KEYS}
    return [k for k in KEYS if (p := repo.objects[k].pin) and p.ref in tags[k]]


def test_repair_rebuilds_only_the_selections_pins_and_branches(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repair` took no keys, so `recover KEY...` steered to a repair of
    every object, reaching the stores the run had left out."""
    repo.commit("baseline")
    repo.new(bookmark="work", eager=True)
    store = default_store()
    lost = _lose_pins(repo, KEYS)
    refs = dict(repo.workspace.working_refs)
    for key in KEYS:
        del store.system(_systems(repo, [key])[0]).branches[refs[key]]
    calls = _spy(monkeypatch, repo.backend_for("memory"), "verify")

    picked = ["db", *KEYS[2:5]]
    plan = repo.plan_repair(["zarr/", "db", "zarr/imaging"])
    assert plan.context["keys"] == picked
    assert sorted((a.op, a.key) for a in plan.actions) == sorted(
        [("repin", k) for k in picked] + [("refork", k) for k in picked]
    )
    assert {p.key for p in plan.preconditions if p.key} == set(picked)
    assert sorted(calls) == _systems(repo, picked)
    report = repo.apply_repair(plan)
    assert report.repinned == {k: lost[k] for k in picked} and not report.failed
    assert sorted(report.reforked) == picked
    assert _pinned(repo) == picked
    for key in KEYS:
        branches = store.system(_systems(repo, [key])[0]).branches
        assert (refs[key] in branches) == (key in picked), key

    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.plan_repair(["db", "nope"])
    with pytest.raises(ConfigError, match="no object under nope/"):
        repo.repair(["nope/"])
    rest = repo.plan_repair()
    assert "keys" not in rest.context
    assert sorted({a.key for a in rest.actions}) == ["raw/plate1", "zarrish"]
    assert sorted(repo.repair(["raw/", "zarrish"]).repinned) == [
        "raw/plate1",
        "zarrish",
    ]
    assert _pinned(repo) == KEYS


def test_repair_all_history_selects_from_every_key_history_has_had(
    repo: Repo,
) -> None:
    repo.commit("baseline")
    lost = _lose_pins(repo, ["db", "raw/plate1", "zarrish"])
    repo.remove("raw/plate1")
    repo.commit("raw/plate1 removed")

    with pytest.raises(ConfigError, match="no object under raw/"):
        repo.plan_repair(["raw/"])
    plan = repo.plan_repair(["raw/"], all_history=True)
    assert plan.context["keys"] == ["raw/plate1"]
    ((op, label, target),) = [(a.op, a.key, a.target) for a in plan.actions]
    assert op == "repin" and label.startswith("raw/plate1@")
    assert target.endswith(lost["raw/plate1"])
    plan = repo.plan_repair(["db", "zarr/"], all_history=True)
    assert [a.key for a in plan.actions] == ["db"]  # once, though at every commit
    with pytest.raises(ConfigError, match="no such object: nope"):
        repo.plan_repair(["nope"], all_history=True)
    report = repo.repair(["raw/plate1", "db"], all_history=True)
    assert sorted(report.repinned.values()) == sorted([lost["db"], lost["raw/plate1"]])
    assert [a.key for a in repo.plan_repair(all_history=True).actions] == ["zarrish"]


def test_a_saved_repair_plan_applies_exactly_its_selection(repo: Repo) -> None:
    repo.commit("baseline")
    lost = _lose_pins(repo, ["db", "zarrish"])
    saved = repo.plan_repair(["db"]).to_json()

    # The selection is part of what the digest vouches for.
    for edit in (["db", "zarrish"], ["zarrish"], None):
        data = json.loads(saved)
        if edit is None:
            del data["context"]["keys"]
        else:
            data["context"]["keys"] = edit
        with pytest.raises(StalePlanError, match="edited after it was saved"):
            repo.apply_repair(Plan.from_json(json.dumps(data)))
    # Keys named at apply must be the plan's.
    with pytest.raises(ConfigError, match="repairs db, not zarrish"):
        repo.apply_repair(Plan.from_json(saved), keys=["zarrish"])
    with pytest.raises(ConfigError, match="repairs every object, not db"):
        repo.apply_repair(repo.plan_repair(), keys=["db"])
    assert _pinned(repo) == [k for k in KEYS if k not in lost]

    report = repo.apply_repair(Plan.from_json(saved), keys=["db"])
    assert report.repinned == {"db": lost["db"]}
    assert _pinned(repo) == [k for k in KEYS if k != "zarrish"]


def test_cli_repair_takes_selectors_and_plans_keep_them(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    monkeypatch.chdir(repo.root)
    repo.commit("baseline")
    lost = _lose_pins(repo, KEYS)
    saved = tmp_path_factory.mktemp("plans") / "repair.json"

    r = runner.invoke(app, ["repair", "zarr/", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["context"]["keys"] == KEYS[2:5]
    r = runner.invoke(app, ["repair", "db", "--plan", str(saved)])
    assert r.exit_code == 0, r.output
    assert json.loads(saved.read_text())["context"]["keys"] == ["db"]
    r = runner.invoke(app, ["repair", "zarrish", "--from-plan", str(saved)])
    assert r.exit_code == 1 and "repairs db, not zarrish" in r.output, r.output
    r = runner.invoke(app, ["repair", "--from-plan", str(saved), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["repinned"] == {"db": lost["db"]}
    r = runner.invoke(app, ["repair", "--", "zarr/imaging", "raw/"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == [
        f"repinned  raw/plate1 -> {lost['raw/plate1']}",
        f"repinned  zarr/imaging -> {lost['zarr/imaging']}",
    ]
    assert _pinned(repo) == ["db", "raw/plate1", "zarr/imaging"]
    r = runner.invoke(app, ["repair", "nope/"])
    assert r.exit_code == 1 and "no object under nope/" in r.output, r.output
    r = runner.invoke(app, ["repair", "--all-history", "zarr/", "--json"])
    assert r.exit_code == 0, r.output
    assert sorted(json.loads(r.output)["repinned"]) == ["zarr/deep/x", "zarr/labels"]
