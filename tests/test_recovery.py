"""Recovering a dataset whose repository was lost, from what its stores hold:
`init --dataset-id`, `restore --at`, `new --adopt`, `recover`."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from tether.backends.memory import default_store
from tether.errors import ConfigError, StalePlanError, TetherError
from tether.manifest import RepoConfig, read_config
from tether.plan import Plan
from tether.repo import Repo

typer_testing = pytest.importorskip("typer.testing")

from tether.cli import app  # noqa: E402

runner = typer_testing.CliRunner()

BAD_IDS = [
    "",
    "0a1b2c3",  # short
    "0a1b2c3d4",  # long
    "0A1B2C3D",  # uppercase
    "0a1B2c3d",
    "0a1b2c3g",  # not hex
    "0a1b-c3d",
    " 0a1b2c3",
    "tether.0a",
]


def _mem(repo: Repo, key: str = "db", system: str | None = None) -> str:
    name = system or f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(name)
    repo.add(key, "memory", {"system": name, "branch": "main"})
    return name


def _state(repo: Repo, key: str) -> str:
    report = repo.status()
    return next(o.state_label for o in report.objects if o.key == key)


def _outside_refs(system: str) -> dict[str, str]:
    """A memory system with refs tether did not make, beside its trunk pin:
    a branch `exp`, a tag `release-1`, and an older state on `exp`.
    Returns ref -> the snapshot it names."""
    store = default_store()
    old = store.write(system, "exp", {"exp": 1})
    head = store.write(system, "exp", {"exp": 2})
    tagged = store.write(system, "scratch", {"tagged": 1})
    sys = store.system(system)
    del sys.branches["scratch"]
    sys.tags["release-1"] = tagged
    return {"exp": head, "release-1": tagged, old: old}


def _refs(system: str, *, but: str = "") -> tuple[dict[str, str], dict[str, str]]:
    sys = default_store().system(system)
    return {b: s for b, s in sys.branches.items() if b != but}, dict(sys.tags)


def _icechunk() -> Any:
    pytest.importorskip("zarr")
    return pytest.importorskip("icechunk")


def _ic_open(uri: str) -> Any:
    ic = _icechunk()
    return ic.Repository.open(ic.local_filesystem_storage(uri))


def _ic_new(path: Path) -> str:
    """A local Icechunk repository with one commit on `main`; its URI."""
    ic = _icechunk()
    import zarr

    repo = ic.Repository.create(ic.local_filesystem_storage(str(path)))
    session = repo.writable_session("main")
    zarr.create_group(store=session.store).attrs["v"] = 0
    session.commit("init")
    return str(path)


def _ic_write(uri: str, branch: str, value: int) -> str:
    """Commit `v = value` on `branch`; the new snapshot id."""
    import zarr

    session = _ic_open(uri).writable_session(branch)
    zarr.open_group(store=session.store, mode="a").attrs["v"] = value
    return str(session.commit(f"v={value}"))


def _ic_read(uri: str, ref: str) -> int:
    import zarr

    repo = _ic_open(uri)
    try:
        session = repo.readonly_session(branch=ref)
    except Exception:
        session = repo.readonly_session(tag=ref)
    attrs: Any = zarr.open_group(store=session.store, mode="r").attrs
    return int(attrs["v"])


def _ic_refs(uri: str, *, but: str = "") -> dict[str, str]:
    repo = _ic_open(uri)
    out = {f"branch {b}": str(repo.lookup_branch(b)) for b in repo.list_branches()}
    out.update({f"tag {t}": str(repo.lookup_tag(t)) for t in repo.list_tags()})
    out.pop(f"branch {but}", None)
    return out


# --------------------------------------------------------------------------- #
# init --dataset-id
# --------------------------------------------------------------------------- #
def test_init_takes_a_dataset_id_and_its_refs_carry_it(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root, dataset_id="0a1b2c3d")
    assert repo.config.dataset_id == "0a1b2c3d"
    assert read_config(vcs_root).dataset_id == "0a1b2c3d"
    _mem(repo)
    pin = repo.commit("baseline").pinned["db"]
    assert pin is not None and pin.ref.startswith("tether.0a1b2c3d.")
    repo.new(bookmark="feat", eager=True)
    assert repo.workspace.working_refs["db"] == "tether.ws.0a1b2c3d.feat"
    # Through `config` too, and `dataset_id` wins over it.
    other = vcs_root / "other"
    made = Repo.init(other, config=RepoConfig(dataset_id="ffffffff"))
    assert made.config.dataset_id == "ffffffff"
    again = vcs_root / "again"
    made = Repo.init(
        again, config=RepoConfig(dataset_id="ffffffff"), dataset_id="12345678"
    )
    assert read_config(again).dataset_id == "12345678"


def test_init_refuses_an_id_that_is_not_eight_lowercase_hex(vcs_root: Path) -> None:
    for bad in BAD_IDS:
        with pytest.raises(ConfigError, match="invalid dataset id"):
            Repo.init(vcs_root, dataset_id=bad)
        with pytest.raises(ConfigError, match="invalid dataset id"):
            Repo.init(vcs_root, config=RepoConfig(dataset_id=bad))
        assert not (vcs_root / "tether.toml").exists(), bad
        assert not (vcs_root / ".tether").exists(), bad


def test_cli_init_dataset_id(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    for bad in ("0A1B2C3D", "0a1b2c3", "xyzxyzxy"):
        r = runner.invoke(app, ["init", "--dataset-id", bad])
        assert r.exit_code == 1 and "invalid dataset id" in r.stderr, r.output
        assert not (vcs_root / "tether.toml").exists()
    r = runner.invoke(app, ["init", "--dataset-id", "0a1b2c3d", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["dataset_id"] == "0a1b2c3d"
    assert Repo.find(vcs_root).config.dataset_id == "0a1b2c3d"


# --------------------------------------------------------------------------- #
# restore --at
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["branch", "tag", "state"])
def test_restore_at_copies_a_native_ref_onto_the_bookmark_branch(
    vcs_root: Path, which: str
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})
    pin = repo.commit("baseline").pinned["db"]
    assert pin is not None
    refs = _outside_refs(system)
    ref = {"branch": "exp", "tag": "release-1"}.get(which) or next(
        r for r in refs if r not in ("exp", "release-1")
    )
    expected = {"snapshot_id": refs[ref]}
    repo.new(bookmark="feat")  # lazy: nothing forked yet
    wref = f"tether.ws.{repo.config.dataset_id}.feat"
    before = _refs(system)

    plan = repo.plan_restore(["db"], at=ref)
    (fork,) = plan.actions
    assert fork.op == "fork" and fork.target == wref, fork
    assert fork.params["at"] == ref and fork.params["at_state"] == expected
    assert plan.context["at"] == ref and plan.context["from_rev"] is None
    assert [p.kind for p in plan.preconditions if p.key == "db"] == ["ref_absent"]
    assert repo.apply_restore(plan) == {"db": wref}

    assert store.system(system).branches[wref] == refs[ref]
    assert _refs(system, but=wref) == before  # REF itself never moved
    assert repo.workspace.working_refs["db"] == wref
    assert repo.workspace.fork_points["db"] == expected
    assert not repo.is_stale()
    assert _state(repo, "db") == "modified"  # against the trunk pin
    assert repo.ops()[0].summary() == f"restored db from {ref}"

    # Writes land on the copy; the next commit pins them.
    head = store.write(system, wref, {"more": 1})
    assert _refs(system, but=wref) == before
    res = repo.commit("from the outside ref")
    moved = res.pinned["db"]
    assert moved is not None and moved.id != pin.id
    assert repo.objects["db"].state == {"snapshot_id": head}
    assert _state(repo, "db") == "clean"


def test_restore_at_on_a_local_icechunk_store(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A branch, a tag and a snapshot id made with Icechunk itself, each copied
    onto the bookmark's branch in turn; none of them moves."""
    _icechunk()
    uri = _ic_new(tmp_path_factory.mktemp("stores") / "imaging.icechunk")
    repo = Repo.init(vcs_root)
    repo.add(None, "icechunk", {"uri": uri})
    pin = repo.commit("baseline").pinned["imaging"]
    assert pin is not None
    ic_repo = _ic_open(uri)
    ic_repo.create_branch("exp", ic_repo.lookup_branch("main"))
    older = _ic_write(uri, "exp", 1)
    _ic_write(uri, "exp", 2)
    ic_repo.create_tag("release-1", older)
    repo.new(bookmark="feat")
    wref = f"tether.ws.{repo.config.dataset_id}.feat"
    before = _ic_refs(uri)

    for ref, value in (("exp", 2), ("release-1", 1), (older, 1), ("main", 0)):
        plan = repo.plan_restore(["imaging"], at=ref)
        (fork,) = plan.actions
        assert fork.op == "fork" and fork.target == wref, fork.detail
        assert repo.apply_restore(plan) == {"imaging": wref}
        state = {"snapshot_id": str(_ic_open(uri).lookup_branch(wref))}
        assert fork.params["at_state"] == state
        assert repo.workspace.fork_points["imaging"] == state
        assert _ic_refs(uri, but=wref) == before, ref
        assert _ic_read(uri, wref) == value
    assert _state(repo, "imaging") == "clean"  # `main`: back at the pin

    repo.restore(["imaging"], at="exp")
    assert _state(repo, "imaging") == "modified"
    head = _ic_write(uri, wref, 3)
    res = repo.commit("from exp")
    moved = res.pinned["imaging"]
    assert moved is not None and moved.id != pin.id and moved.created
    assert str(_ic_open(uri).lookup_tag(moved.ref)) == head
    assert _ic_read(uri, "exp") == 2  # the original is untouched


def test_restore_at_resolves_at_plan_time_and_the_saved_plan_binds_it(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    repo.commit("baseline")
    refs = _outside_refs(system)
    repo.new(bookmark="feat")
    wref = f"tether.ws.{repo.config.dataset_id}.feat"
    saved = repo.plan_restore(["db"], at="exp").to_json()

    # An edited plan fails its digest, whichever part was changed.
    for edit in ("at_state", "at", "context"):
        data = json.loads(saved)
        if edit == "context":
            data["context"]["at"] = "release-1"
        else:
            params = data["actions"][0]["params"]
            params[edit] = (
                {"snapshot_id": refs["release-1"]} if edit == "at_state" else "x"
            )
        with pytest.raises(StalePlanError, match="edited after it was saved"):
            repo.apply_restore(Plan.from_dict(data))
        assert wref not in store.system(system).branches

    # `exp` moves after the plan: the plan forks what it reviewed.
    later = store.write(system, "exp", {"exp": 3})
    assert repo.apply_restore(Plan.from_json(saved)) == {"db": wref}
    assert store.system(system).branches[wref] == refs["exp"] != later
    # Applied once, the same plan is stale: the branch it found absent exists.
    with pytest.raises(StalePlanError, match="exists since the plan was made"):
        repo.apply_restore(Plan.from_json(saved))


def test_restore_at_refuses_unpinned_writes_without_discard(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    repo.commit("baseline")
    refs = _outside_refs(system)
    repo.new(bookmark="feat", eager=True)
    wref = repo.workspace.working_refs["db"]

    # A branch at the pin holds nothing to lose: reset without --discard.
    plan = repo.plan_restore(["db"], at="release-1")
    (fork,) = plan.actions
    assert fork.op == "fork" and fork.params["existing"] == wref
    assert [p.kind for p in plan.preconditions if p.key == "db"] == ["ref_head"]
    repo.apply_restore(plan)
    assert store.system(system).branches[wref] == refs["release-1"]

    # Writes since: refused, and the branch keeps them.
    scratch = store.write(system, wref, {"scratch": 1})
    plan = repo.plan_restore(["db"], at="exp")
    (refused,) = plan.actions
    assert refused.op == "refuse" and "has writes since" in refused.detail
    assert "--discard" in refused.detail
    with pytest.raises(TetherError, match="cannot restore"):
        repo.apply_restore(plan)
    assert store.system(system).branches[wref] == scratch
    # --discard throws them away on purpose, from the head it reviewed.
    plan = repo.plan_restore(["db"], at="exp", discard=True)
    (fork,) = plan.actions
    assert fork.op == "fork" and "discarding its writes" in fork.detail
    store.write(system, wref, {"scratch": 2})  # moved after the plan
    with pytest.raises(StalePlanError, match="moved since the plan was made"):
        repo.apply_restore(plan)
    repo.restore(["db"], at="exp", discard=True)
    assert store.system(system).branches[wref] == refs["exp"]


def test_restore_at_refusals(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    raw = tmp_path_factory.mktemp("raw") / "plate.csv"
    raw.write_text("a,b\n", encoding="utf-8")
    repo.add("raw", "file", {"uri": str(raw)})
    repo.commit("baseline")
    _outside_refs(system)
    before = _refs(system)

    # One source, not two, not none.
    with pytest.raises(ConfigError, match="one source"):
        repo.plan_restore(["db"], "@", at="exp")
    with pytest.raises(ConfigError, match="one source"):
        repo.plan_restore(["db"])

    # The trunk: its working ref is the upstream branch, never moved.
    assert repo.on_trunk()
    (refused,) = repo.plan_restore(["db"], at="exp").actions
    assert refused.op == "refuse" and "trunk" in refused.detail
    assert "add ... --at REF" in refused.detail and "tether pull" in refused.detail
    with pytest.raises(TetherError, match="cannot restore"):
        repo.restore(["db"], at="exp")
    assert _refs(system) == before

    # Off the trunk: an object that cannot fork, and a ref its store lacks.
    repo.new(bookmark="feat")
    plan = repo.plan_restore(["db", "raw"], at="exp")
    ops = {a.key: a for a in plan.actions}
    assert ops["db"].op == "fork"
    assert ops["raw"].op == "refuse" and "cannot fork" in ops["raw"].detail
    (unknown,) = repo.plan_restore(["db"], at="no-such-ref").actions
    assert unknown.op == "refuse" and "cannot resolve 'no-such-ref'" in unknown.detail
    assert _refs(system) == before


def test_cli_restore_at(
    vcs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    monkeypatch.chdir(vcs_root)
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    repo.commit("baseline")
    refs = _outside_refs(system)
    repo.new(bookmark="feat")
    wref = f"tether.ws.{repo.config.dataset_id}.feat"

    for args in (["--at", "exp", "--from", "@"], []):
        r = runner.invoke(app, ["restore", "db", *args])
        assert r.exit_code == 1 and "--from REV or --at REF" in r.stderr, r.output
    r = runner.invoke(app, ["restore", "db", "--at", "exp", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["context"]["at"] == "exp"
    plan_file = tmp_path_factory.mktemp("plans") / "restore.json"
    r = runner.invoke(app, ["restore", "db", "--at", "exp", "--plan", str(plan_file)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["restore", "db", "--at", "release-1", "--from-plan", str(plan_file)]
    )
    assert r.exit_code == 1 and "made with --at exp" in r.stderr, r.output
    r = runner.invoke(app, ["restore", "db", "--from-plan", str(plan_file), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {
        "from": None,
        "at": "exp",
        "working_refs": {"db": wref},
    }
    assert default_store().system(system).branches[wref] == refs["exp"]
    r = runner.invoke(app, ["restore", "db", "--at", "release-1"])
    assert r.exit_code == 0, r.output
    assert f"db -> {wref}  (from release-1; release-1 is untouched)" in r.stdout
