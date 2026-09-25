"""Selecting objects by key or prefix: one helper, every command that takes keys."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from tether.backends.memory import MemoryBackend, default_store
from tether.errors import ConfigError
from tether.handles import MemoryHandle
from tether.manifest import compute_pin_id, write_object
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
