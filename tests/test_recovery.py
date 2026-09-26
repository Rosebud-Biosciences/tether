"""Recovering a dataset whose repository was lost, from what its stores hold:
`init --dataset-id`, `restore --at`, `new --adopt`, `recover`."""

from __future__ import annotations

import json
import subprocess
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


def _recovered(
    repo: Repo, tmp: pytest.TempPathFactory, *, dataset_id: str | None = None
) -> Repo:
    """`repo`'s repository lost: a fresh one of the same VCS elsewhere, with
    the dataset initialized under `dataset_id` (default: `repo`'s) and the
    same objects registered."""
    root = tmp.mktemp("recovered")
    if repo.vcs.kind == "git":
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    else:
        subprocess.run(["jj", "git", "init"], cwd=root, check=True, capture_output=True)
    fresh = Repo.init(root, dataset_id=dataset_id or repo.config.dataset_id)
    for key, m in sorted(repo.objects.items()):
        fresh.add(key, m.kind, dict(m.locator))
    return fresh


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


# --------------------------------------------------------------------------- #
# new --adopt
# --------------------------------------------------------------------------- #
def _lost_bookmark(vcs_root: Path) -> tuple[Repo, str, str]:
    """A dataset with bookmark `feat` whose branch holds a committed write
    and an uncommitted one on top: (repo, system, head)."""
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    store = default_store()
    store.write(system, "main", {"v": 1})
    repo.commit("baseline")
    repo.new(bookmark="feat", eager=True)
    wref = repo.workspace.working_refs["db"]
    store.write(system, wref, {"v": 2})
    repo.commit("feat: v2")
    return repo, system, store.write(system, wref, {"v": 3})


def test_new_adopt_takes_a_branch_with_uncommitted_writes_as_it_is(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    lost, system, head = _lost_bookmark(vcs_root)
    store = default_store()
    wref = lost.workspace.working_refs["db"]
    repo = _recovered(lost, tmp_path_factory)
    repo.commit("recover the trunk")

    # Without --adopt the branch is somebody's unpinned writes: refused.
    (refused,) = repo.plan_new(bookmark="feat").actions
    assert refused.op == "refuse" and "has writes since" in refused.detail
    with pytest.raises(TetherError, match="unpinned writes"):
        repo.new(bookmark="feat")
    assert store.system(system).branches[wref] == head
    with pytest.raises(ConfigError, match="--discard would reset them"):
        repo.plan_new(bookmark="feat", adopt=True, discard=True)
    with pytest.raises(ConfigError, match="--keep keeps"):
        repo.plan_new(adopt=True, keep=True)

    plan = repo.plan_new(bookmark="feat", adopt=True)
    (taken,) = plan.actions
    assert taken.op == "adopt" and taken.target == wref
    assert taken.params["head"] == {"snapshot_id": head}
    assert taken.params["generation"] is None
    assert "adopted as it is" in taken.detail and "next commit pins" in taken.detail
    (bound,) = [p for p in plan.preconditions if p.key == "db"]
    assert bound.kind == "ref_present" and bound.expected is None
    assert bound.params["ref"] == wref
    assert plan.is_empty  # nothing is written to the store
    repo.apply_new(plan)

    assert store.system(system).branches[wref] == head  # no reset, no copy
    assert repo.workspace.bookmark == "feat"
    assert repo.workspace.working_refs["db"] == wref
    assert repo.workspace.fork_points["db"] == repo.objects["db"].state
    assert not repo.is_stale()
    assert repo.ops()[0].summary().startswith("adopted db")
    assert _state(repo, "db") == "modified"
    res = repo.commit("feat: recovered")
    pin = res.pinned["db"]
    assert pin is not None and pin.created
    assert repo.objects["db"].state == {"snapshot_id": head}
    assert _state(repo, "db") == "clean"


def test_new_adopt_takes_the_highest_generation_and_forks_the_rest(
    vcs_root: Path,
) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    fresh = _mem(repo, "fresh")
    store = default_store()
    repo.commit("baseline")
    ds = repo.config.dataset_id
    sys = store.system(system)
    base = sys.branches["main"]
    gens = {
        f"tether.ws.{ds}.feat": store.write(system, "g1", {"g": 1}),
        f"tether.ws.{ds}.feat.3": store.write(system, "g3", {"g": 3}),
        f"tether.ws.{ds}.feat.2": store.write(system, "g2", {"g": 2}),
    }
    for name, sid in gens.items():
        sys.branches[name] = sid
    # Not this bookmark's, or not this dataset's: never adopted.
    sys.branches[f"tether.ws.{ds}.feature"] = base
    sys.branches[f"tether.ws.{'f' * 8}.feat.9"] = base
    before = dict(sys.branches)

    plan = repo.plan_new(bookmark="feat", adopt=True, eager=True)
    ops = {a.key: a for a in plan.actions}
    assert ops["db"].op == "adopt" and ops["db"].target == f"tether.ws.{ds}.feat.3"
    assert ops["db"].params["generation"] == 3
    assert "generation 3, the highest of 3" in ops["db"].detail
    # An object whose store has no such branch forks as `new` always does.
    assert ops["fresh"].op == "fork" and ops["fresh"].target == f"tether.ws.{ds}.feat"
    assert {(p.key, p.kind) for p in plan.preconditions if p.key} == {
        ("db", "ref_present"),
        ("fresh", "ref_absent"),
    }
    repo.apply_new(plan)
    assert repo.workspace.working_refs == {
        "db": f"tether.ws.{ds}.feat.3",
        "fresh": f"tether.ws.{ds}.feat",
    }
    assert store.system(system).branches == before
    assert (
        store.system(fresh).branches[f"tether.ws.{ds}.feat"]
        == (store.system(fresh).branches["main"])
    )
    # Undo takes back the fork it made and leaves the adopted branch alone.
    report = repo.undo()
    assert report.op.command == "new"
    assert f"tether.ws.{ds}.feat" not in store.system(fresh).branches
    assert store.system(system).branches == before

    # Lazily, the object without a branch defers its fork.
    repo.new("main")
    plan = repo.plan_new(bookmark="feat2", adopt=True)
    assert {a.key: a.op for a in plan.actions} == {
        "db": "defer-fork",
        "fresh": "defer-fork",
    }


@pytest.mark.parametrize(
    "between", ["writes", "unrelated refs", "branch deleted", "newer generation"]
)
def test_a_saved_new_adopt_plan_binds_to_the_branch_not_its_head(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory, between: str
) -> None:
    """Adopting moves nothing: the branch is taken whatever its head. A plan
    bound to the head it saw was refused whenever the branch was written in
    between, so a branch something keeps writing could never be adopted. It
    binds to the branch still being there and still the newest generation of
    the bookmark's branch -- a sibling a store made since is where whoever
    made it writes, and the adopted one is stale."""
    lost, system, head = _lost_bookmark(vcs_root)
    store = default_store()
    sys = store.system(system)
    wref = lost.workspace.working_refs["db"]
    ds = lost.config.dataset_id
    repo = _recovered(lost, tmp_path_factory)
    repo.commit("recover the trunk")
    saved = repo.plan_new(bookmark="feat", adopt=True).to_json()
    assert json.loads(saved)["context"]["adopt"] is True

    if between in ("writes", "unrelated refs"):
        moved = store.write(system, wref, {"v": 4})  # the lost checkout writes on
        if between == "unrelated refs":
            # Newer generations, but of another bookmark's or dataset's branch.
            sys.branches[f"tether.ws.{ds}.feature.5"] = head
            sys.branches[f"tether.ws.{'f' * 8}.feat.5"] = head
        repo.apply_new(Plan.from_json(saved))
        assert repo.workspace.bookmark == "feat"
        assert repo.workspace.working_refs["db"] == wref
        assert sys.branches[wref] == moved  # no reset, no copy
        repo.commit("feat: recovered")
        assert repo.objects["db"].state == {"snapshot_id": moved}
        return

    if between == "branch deleted":
        del sys.branches[wref]
        expected = f"new db: {wref} is gone since the plan was made"
    else:
        sys.branches[f"{wref}.2"] = store.write(system, "g2", {"g": 2})
        expected = f"new db: {wref} is superseded by {wref}.2"
    with pytest.raises(StalePlanError) as exc:
        repo.apply_new(Plan.from_json(saved))
    assert expected in str(exc.value) and "re-run the plan" in str(exc.value)
    assert repo.workspace.bookmark == "main" and "feat" not in repo.vcs.bookmarks()
    # Planned again, the bookmark's branch as the store has it now.
    repo.new(bookmark="feat", adopt=True)
    if between == "branch deleted":
        assert repo.workspace.pending_forks["db"] == wref
    else:
        assert repo.workspace.working_refs["db"] == f"{wref}.2"


def test_new_adopt_refusals(vcs_root: Path, tmp_path: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _mem(repo)
    repo.commit("baseline")
    ds = repo.config.dataset_id
    default_store().system(system).branches[f"tether.ws.{ds}.main"] = "x"

    # The trunk: its working refs are the upstream branches.
    for plan in (repo.plan_new("main", adopt=True), repo.plan_new(adopt=True)):
        (refused,) = plan.actions
        assert refused.op == "refuse" and not refused.key
        assert "the trunk 'main' has none" in refused.detail
    with pytest.raises(TetherError, match="own store branches"):
        repo.new(adopt=True)
    assert repo.workspace.bookmark == "main"

    # Another live checkout holding the bookmark.
    repo.new(bookmark="feat")
    other = tmp_path / "peer"
    if repo.vcs.kind == "jj":
        subprocess.run(
            ["jj", "workspace", "add", str(other)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(other)],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    peer = Repo.find(other)
    (refused,) = peer.plan_new("feat", adopt=True).actions
    assert refused.op == "refuse" and "held by live workspace" in refused.detail


def test_cli_new_adopt(
    vcs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    lost, _system, _head = _lost_bookmark(vcs_root)
    wref = lost.workspace.working_refs["db"]
    repo = _recovered(lost, tmp_path_factory)
    repo.commit("recover the trunk")
    monkeypatch.chdir(repo.root)
    r = runner.invoke(app, ["new", "--adopt"])
    assert r.exit_code == 1 and "own store branches" in r.stderr, r.output
    r = runner.invoke(app, ["new", "-b", "feat", "--adopt", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "adopt" in r.stdout and wref in r.stdout
    r = runner.invoke(app, ["new", "-b", "feat", "main", "--adopt", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["adopted"] == {"db": wref}
    assert payload["working_refs"] == {"db": wref}
    runner.invoke(app, ["new", "main"])
    r = runner.invoke(app, ["new", "feat", "--adopt"])
    assert r.exit_code == 0, r.output
    assert f"db -> {wref}  (adopted as it is; at snapshot_id=" in r.stdout


# --------------------------------------------------------------------------- #
# recover
# --------------------------------------------------------------------------- #
def test_recover_groups_refs_by_dataset_and_says_what_to_run(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    lost, system, _head = _lost_bookmark(vcs_root)
    old = lost.config.dataset_id
    store = default_store()
    sys = store.system(system)
    base = sys.branches["main"]
    sys.branches[f"tether.ws.{old}.exp"] = base
    sys.branches[f"tether.ws.{old}.exp.2"] = base
    sys.tags["release-1"] = base  # not tether's: not listed
    raw = tmp_path_factory.mktemp("raw") / "plate.csv"
    raw.write_text("a,b\n", encoding="utf-8")
    lost.add("raw", "file", {"uri": str(raw)})

    repo = _recovered(lost, tmp_path_factory, dataset_id="12345678")
    report = repo.recover_report()
    assert [o.key for o in report.objects] == ["db", "raw"]
    db, raw_row = report.objects
    assert not raw_row.holds_refs and not raw_row.namespaces
    assert set(db.namespaces) == {old}
    refs = db.namespaces[old]
    assert len(refs.pins) == 2  # the trunk's and feat's
    assert refs.bookmarks == {
        "feat": [f"tether.ws.{old}.feat"],
        "exp": [f"tether.ws.{old}.exp", f"tether.ws.{old}.exp.2"],
    }
    assert not report.pinned and report.suggested_id == old
    assert report.steps == [
        "set this dataset's id in tether.toml (nothing is pinned under "
        f'12345678 yet):\n[dataset]\nid = "{old}"',
        'tether commit -m "Recover main"',
        'tether new -b exp main --adopt && tether commit -m "Recover exp"',
        'tether new -b feat main --adopt && tether commit -m "Recover feat"',
    ]
    assert repo.recover_report(["raw"]).objects == [raw_row]
    with pytest.raises(ConfigError, match="no such object"):
        repo.recover_report(["nope"])

    # Another dataset shares the store: grouped apart, and the id is no
    # longer one to guess.
    other = "fedcba98"
    sys.tags[f"tether.{other}.00000000000000aa"] = base
    sys.branches[f"tether.ws.{other}.side"] = base
    report = repo.recover_report()
    assert set(report.datasets) == {old, other}
    assert report.datasets[other].bookmarks == {"side": [f"tether.ws.{other}.side"]}
    assert report.suggested_id is None
    assert f'under [dataset]:\nid = "{min(old, other)}"' in report.steps[0]
    assert f'\nid = "{max(old, other)}"' in report.steps[0]
    assert "per bookmark of that id" in report.steps[-1]

    # Under the old id, it is this dataset's: no id to set.
    again = _recovered(lost, tmp_path_factory)
    report = again.recover_report(["db"])
    assert report.steps[0] == 'tether commit -m "Recover main"'
    assert report.to_dict()["datasets"][0] == {
        "dataset_id": old,
        "current": True,
        "pins": 2,
        "bookmarks": [
            {
                "bookmark": "exp",
                "branch": f"tether.ws.{old}.exp.2",
                "generation": 2,
                "branches": [f"tether.ws.{old}.exp", f"tether.ws.{old}.exp.2"],
            },
            {
                "bookmark": "feat",
                "branch": f"tether.ws.{old}.feat",
                "generation": None,
                "branches": [f"tether.ws.{old}.feat"],
            },
        ],
    }

    # Once this dataset has pinned under its own id, the id stays.
    repo.commit("pinned under the new id")
    report = repo.recover_report(["db"])
    assert report.pinned and report.suggested_id is None
    assert not any("[dataset]" in step for step in report.steps)
    last = report.steps[-1]
    assert f"{', '.join(sorted([old, other]))} are other datasets'" in last
    assert "can no longer change" in last and "tether init --dataset-id ID" in last


def test_cli_recover(
    vcs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    lost, _system, _head = _lost_bookmark(vcs_root)
    old = lost.config.dataset_id
    gone = _mem(lost, "gone")
    repo = _recovered(lost, tmp_path_factory, dataset_id="12345678")
    monkeypatch.chdir(repo.root)

    r = runner.invoke(app, ["recover", "db"])
    assert r.exit_code == 0, r.output
    assert "dataset id 12345678 (tether.toml)" in r.stdout
    assert f"{old}: 2 pin(s); bookmarks: feat" in r.stdout
    assert "(this dataset)" not in r.stdout
    assert f'       id = "{old}"' in r.stdout
    assert "tether new -b feat main --adopt" in r.stdout

    r = runner.invoke(app, ["recover", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert set(payload) == {
        "dataset_id",
        "trunk",
        "pinned",
        "suggested_dataset_id",
        "objects",
        "datasets",
        "steps",
    }
    assert payload["suggested_dataset_id"] == old
    rows = {o["key"]: o for o in payload["objects"]}
    assert rows["gone"] == {
        "key": "gone",
        "kind": "memory",
        "holds_refs": True,
        "error": None,
        "datasets": [],
    }
    (found,) = rows["db"]["datasets"]
    assert found["dataset_id"] == old and not found["current"]

    # A store that cannot be listed is an error row, and exit 1.
    default_store().deleted.add(gone)
    try:
        r = runner.invoke(app, ["recover"])
        assert r.exit_code == 1 and "gone: " in r.stderr, r.output
        assert "gone  [memory]  error" in r.stdout
    finally:
        default_store().deleted.discard(gone)


def test_recover_suggests_nothing_while_a_store_cannot_be_listed(
    vcs_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Steps drawn from a partial listing mislead: with every store failing
    they said there was nothing to take back, and with some failing they
    could name the wrong id or miss bookmarks. Until every selected store
    lists, the only step is to fix access or leave those objects out."""
    lost, _system, _head = _lost_bookmark(vcs_root)
    gone = _mem(lost, "gone")
    repo = _recovered(lost, tmp_path_factory, dataset_id="12345678")
    default_store().deleted.add(gone)
    try:
        for keys in (["gone"], None):  # every selected store failing; one of two
            report = repo.recover_report(keys)
            assert [o.key for o in report.objects if o.error] == ["gone"]
            assert report.suggested_id is None
            (step,) = report.steps
            assert step.startswith("could not list the refs of gone")
            assert "no tether refs" not in step and "commit" not in step
    finally:
        default_store().deleted.discard(gone)
    assert repo.recover_report().suggested_id == lost.config.dataset_id


# --------------------------------------------------------------------------- #
# End to end: the repository is lost, the stores are not
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["memory", "icechunk"])
def test_recover_a_lost_dataset_end_to_end(
    vcs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    kind: str,
) -> None:
    store = default_store()
    if kind == "icechunk":
        _icechunk()
        uri = _ic_new(tmp_path_factory.mktemp("stores") / "imaging.icechunk")
        key, add = "imaging", [uri, "--kind", "icechunk"]

        def write(branch: str, value: int) -> None:
            _ic_write(uri, branch, value)

        def head(branch: str) -> str:
            return str(_ic_open(uri).lookup_branch(branch))

        def tags() -> set[str]:
            return set(_ic_open(uri).list_tags())

        def read(branch: str) -> Any:
            return _ic_read(uri, branch)
    else:
        system = f"sys-{uuid.uuid4().hex[:8]}"
        store.system(system)
        key, add = "db", ["db", "--kind", "memory", "--set", f"system={system}"]

        def write(branch: str, value: int) -> None:
            store.write(system, branch, {"v": value})

        def head(branch: str) -> str:
            return store.system(system).branches[branch]

        def tags() -> set[str]:
            return set(store.system(system).tags)

        def read(branch: str) -> Any:
            return store.read(system, branch)["v"]

    # The dataset as it was: a trunk commit, and a bookmark with a committed
    # write and an uncommitted one on top.
    monkeypatch.chdir(vcs_root)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["add", *add]).exit_code == 0
    write("main", 1)
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0
    assert runner.invoke(app, ["new", "-b", "feat", "--eager"]).exit_code == 0
    lost = Repo.find(vcs_root)
    old, wref = lost.config.dataset_id, lost.workspace.working_refs[key]
    write(wref, 2)
    assert runner.invoke(app, ["commit", "-m", "feat: 2"]).exit_code == 0
    feat_pin = Repo.find(vcs_root).objects[key].pin
    assert feat_pin is not None
    write(wref, 3)  # never committed
    uncommitted = head(wref)
    before = tags()

    # The repository is lost; a fresh one elsewhere, the old id, the object.
    root = tmp_path_factory.mktemp("recovered")
    if lost.vcs.kind == "git":
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    else:
        subprocess.run(["jj", "git", "init"], cwd=root, check=True, capture_output=True)
    monkeypatch.chdir(root)
    r = runner.invoke(app, ["init", "--dataset-id", old])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["add", *add])
    assert r.exit_code == 0 and f"added {key}" in r.stdout, r.output

    r = runner.invoke(app, ["recover", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    (found,) = payload["datasets"]
    assert found["dataset_id"] == old and found["current"] and found["pins"] == 2
    assert [b["bookmark"] for b in found["bookmarks"]] == ["feat"]
    assert payload["steps"] == [
        'tether commit -m "Recover main"',
        'tether new -b feat main --adopt && tether commit -m "Recover feat"',
    ]

    # The trunk re-commits the state it pinned: the tag is reused, none made.
    res = Repo.find(root).commit("Recover main")
    pin = res.pinned[key]
    assert pin is not None and not pin.created
    assert tags() == before

    r = runner.invoke(app, ["new", "-b", "feat", "main", "--adopt"])
    assert r.exit_code == 0, r.output
    assert f"{key} -> {wref}  (adopted as it is;" in r.stdout
    assert head(wref) == uncommitted and read(wref) == 3  # the write survived
    r = runner.invoke(app, ["status", "--json"])
    (row,) = json.loads(r.stdout)["objects"]
    assert row["state"] == "modified"
    res = Repo.find(root).commit("Recover feat")
    pin = res.pinned[key]
    assert pin is not None and pin.created
    assert tags() - before == {pin.ref}
    repo = Repo.find(root)
    assert (repo.objects[key].state or {}).get("snapshot_id") == uncommitted
    assert _state(repo, key) == "clean"

    # What the lost history pinned on the bookmark is no manifest's now: kept,
    # since this clone did not make it, until `gc --release-foreign`.
    kept = [a for a in repo.plan_gc().actions if a.op == "keep-pin"]
    assert [a.params["pin_id"] for a in kept] == [feat_pin.id]
    released = repo.plan_gc(release_foreign=True).actions
    assert [a.params["pin_id"] for a in released if a.op == "unpin"] == [feat_pin.id]
