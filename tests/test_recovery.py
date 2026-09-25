"""Recovering a dataset whose repository was lost, from what its stores hold:
`init --dataset-id`, `restore --at`, `new --adopt`, `recover`."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import ConfigError
from tether.manifest import RepoConfig, read_config
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
