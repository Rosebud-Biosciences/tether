"""`new --defer-record`: a `pin = "record"` object's fork left for the first
writable open, like a pinned one's, for bookmarks that end before the store's
retention would expire the recorded state -- and the warnings that say what
holds that state meanwhile."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from typer import testing as typer_testing

from tether.backends.memory import default_store
from tether.cli import app
from tether.errors import BackendError, ConfigError, TetherError
from tether.handles import MemoryHandle
from tether.manifest import Policy, RepoConfig
from tether.repo import Repo

runner = typer_testing.CliRunner()


def _preview_dataset(vcs_root: Path) -> tuple[Repo, dict[str, str]]:
    """Two committed `pin = "record"` stores: key -> memory system."""
    repo = Repo.init(vcs_root)
    systems: dict[str, str] = {}
    for key in ("touched", "untouched"):
        system = f"sys-{uuid.uuid4().hex[:8]}"
        default_store().write(system, "main", {key: 1})
        repo.add(
            key,
            "memory",
            {"system": system, "branch": "main"},
            policy=Policy(pin="record"),
        )
        systems[key] = system
    repo.commit("baseline")
    return repo, systems


def test_a_preview_branches_only_the_stores_it_writes(vcs_root: Path) -> None:
    """The use case: a short-lived bookmark that writes one of two stores
    leaves the other store untouched, where `new` used to branch both."""
    repo, systems = _preview_dataset(vcs_root)
    store = default_store()
    ops = {a.key: a.op for a in repo.plan_new(bookmark="preview").actions}
    assert ops == {"touched": "fork", "untouched": "fork"}

    plan = repo.plan_new(bookmark="preview", defer_record=True)
    assert {a.key: a.op for a in plan.actions} == {
        "touched": "defer-fork",
        "untouched": "defer-fork",
    }
    assert all("nothing of tether's holds it" in a.detail for a in plan.actions)
    warnings = plan.context["unprotected"]
    assert sorted(warnings) == ["touched", "untouched"]
    assert "its upstream branch still sits at" in warnings["untouched"]
    assert "reading untouched on this bookmark fails too" in warnings["untouched"]
    assert f"warning: untouched: {warnings['untouched']}" in plan.notes

    repo.apply_new(plan)
    assert repo.workspace.working_refs == {}
    assert repo.unprotected_keys() == ["touched", "untouched"]
    assert repo.status(do_snapshot=False).unprotected == ["touched", "untouched"]

    # Reads go to the recorded state; the first writable open forks from it.
    ro = repo.open("untouched", read_only=True)
    assert isinstance(ro, MemoryHandle) and ro.read() == {"untouched": 1}
    handle = repo.open("touched")
    assert isinstance(handle, MemoryHandle) and not handle.read_only
    handle.write({"touched": 2})
    assert repo.unprotected_keys() == ["untouched"]
    repo.commit("preview write")

    name = repo.workspace.pending_forks["untouched"]
    assert name not in store.system(systems["untouched"]).branches
    assert store.read(systems["touched"], "main") == {"touched": 1}


def test_a_deferred_record_fork_fails_once_the_store_expires_its_state(
    vcs_root: Path,
) -> None:
    repo, systems = _preview_dataset(vcs_root)
    store = default_store()
    system = systems["untouched"]
    state = repo.objects["untouched"].state
    assert state is not None
    store.write(system, "main", {"untouched": 2})  # main moves on past it

    plan = repo.plan_new(bookmark="preview", defer_record=True)
    warnings = plan.context["unprotected"]
    assert "has moved on from" in warnings["untouched"]
    assert "only the store's retention keeps it" in warnings["untouched"]
    assert "its upstream branch still sits at" in warnings["touched"]
    repo.apply_new(plan)

    del store.system(system).snapshots[state["snapshot_id"]]  # retention expires it
    with pytest.raises(BackendError):
        expired = repo.open("untouched", read_only=True)
        assert isinstance(expired, MemoryHandle)
        expired.read()
    with pytest.raises(TetherError, match="recorded state is gone"):
        repo.open("untouched")
    assert repo.unprotected_keys() == ["touched", "untouched"]


def test_deferring_record_forks_needs_lazy_forks(vcs_root: Path) -> None:
    repo, _ = _preview_dataset(vcs_root)
    with pytest.raises(ConfigError, match="pick one"):
        repo.plan_new(bookmark="preview", eager=True, defer_record=True)
    repo.config.new_fork = "eager"
    with pytest.raises(ConfigError, match="pass --lazy as well"):
        repo.plan_new(bookmark="preview", defer_record=True)
    plan = repo.plan_new(bookmark="preview", eager=False, defer_record=True)
    assert {a.op for a in plan.actions} == {"defer-fork"}


def test_new_record_config_defers_unless_told_otherwise(vcs_root: Path) -> None:
    repo, _ = _preview_dataset(vcs_root)
    repo.config.new_record = "defer"

    def ops(*, eager: bool | None = None, defer_record: bool | None = None) -> set[str]:
        plan = repo.plan_new(bookmark="preview", eager=eager, defer_record=defer_record)
        return {a.op for a in plan.actions}

    assert ops() == {"defer-fork"}
    assert ops(defer_record=False) == {"fork"}
    assert ops(eager=True) == {"fork"}  # --eager beats the config's deferral


def test_new_record_config_round_trips_and_validates() -> None:
    default = RepoConfig()
    assert "record" not in default.to_toml()  # nothing new in existing files
    deferring = RepoConfig(dataset_id=default.dataset_id, new_record="defer")
    text = deferring.to_toml()
    assert RepoConfig.from_toml(text).new_record == "defer"
    with pytest.raises(ConfigError, match="fork or defer"):
        RepoConfig.from_toml(text.replace('"defer"', '"later"'))
    with pytest.raises(ConfigError, match='set fork = "lazy"'):
        RepoConfig.from_toml(text.replace('fork = "lazy"', 'fork = "eager"'))


def test_cli_warns_at_new_and_at_every_status_until_the_fork(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    _preview_dataset(vcs_root)
    r = runner.invoke(app, ["new", "-b", "preview", "--defer-record"])
    assert r.exit_code == 0, r.output
    assert "untouched -> tether.ws." in r.stdout
    assert "(created on first writable open, no pin)" in r.stdout
    assert "warning: untouched: no pin, and no branch until" in r.stderr

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "warning: untouched has no pin and no branch" in r.stderr
    assert "--no-defer-record` forks it now" in r.stderr
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.stdout)["unprotected"] == ["touched", "untouched"]

    r = runner.invoke(app, ["new", "--no-defer-record", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["bookmark"] == "preview" and out["unprotected"] == {}
    assert sorted(out["working_refs"]) == ["touched", "untouched"]
    r = runner.invoke(app, ["status"])
    assert "no pin and no branch" not in r.stderr
