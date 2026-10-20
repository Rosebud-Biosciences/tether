"""Byte-exact outputs of every format tether hashes or commits.

Pin ids, listing names, the manifest hash and a saved plan's digest are
hashes of these encodings, and the TOML is what a commit records. A change to
any value here changes the pin ids, listing names or plan digests of every
existing dataset, or rewrites its committed files: a format change, not a
refactor. A reimplementation -- a native scanner, a port -- must reproduce
them byte for byte. The inputs aim at the corners: unsorted and non-ASCII
keys, a float, an integer past 2**53, control and line-separator characters,
`None`, symlinks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tether.backends.file import (
    FileBackend,
    _digest_pairs,
    _dump_listing,
    _link_token,
    _load_listing,
)
from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    RepoConfig,
    bookmark_slug,
    canonical_bytes,
    compute_pin_id,
    listing_name,
    manifest_hash,
    slugify_key,
    working_ref_name,
)
from tether.plan import Action, Plan, Precondition
from tether.vcs import _digest_revs

STATE = {"snapshot_id": "1CECHNKREP0F1RSTCMT0", "branch": "main", "n": 3}


def test_canonical_json() -> None:
    """Keys sorted by code point, compact separators, non-ASCII and U+2028
    as raw UTF-8, control characters escaped, a float as Python prints it
    (`1e+16`, where serde_json prints `1e16`), an integer exact past 2**53."""
    obj = {
        "z": [1, 2.5, None, True, False],
        "B": {"é": "値", "a": "\n\t\x01\u2028"},
        "10": 2**53 + 1,
        "9": 1e16,
        "a": "",
    }
    assert canonical_bytes(obj) == (
        b'{"10":9007199254740993,"9":1e+16,'
        b'"B":{"a":"\\n\\t\\u0001\xe2\x80\xa8","\xc3\xa9":"\xe5\x80\xa4"},'
        b'"a":"","z":[1,2.5,null,true,false]}'
    )


def test_pin_id_and_listing_name() -> None:
    identity = {"uri": "s3://lab/runs/greetings.icechunk"}
    assert compute_pin_id("icechunk", identity, STATE, "22620a34") == (
        "22620a34.19acba18aa8fe7ef"
    )
    dir_state = {"type": "dir", "count": 2, "size": 10, "digest": "ab"}
    assert listing_name("file", {"uri": "/data/raw/plate1"}, dir_state) == (
        "bf9cc79f8240c13e5a00.jsonl"
    )


@pytest.mark.parametrize(
    ("bookmark", "slug"),
    [
        ("main", "main"),
        ("a_b-c", "a_b-c"),
        ("feat/x", "feat-x-cdd300"),
        ("feat.x", "feat-x-463316"),
        ("pr 12", "pr-12-036656"),
        ("über-branch", "ber-branch-af0823"),
        ("-", "bm-516783"),
    ],
)
def test_bookmark_slug(bookmark: str, slug: str) -> None:
    assert bookmark_slug(bookmark) == slug


def test_working_ref_name() -> None:
    assert working_ref_name("22620a34", "feat/x") == "tether.ws.22620a34.feat-x-cdd300"


@pytest.mark.parametrize(
    ("key", "slug"),
    [("zarr/imaging", "zarr-imaging"), ("Raw Plates", "raw-plates"), ("a..b", "a-b")],
)
def test_slugify_key(key: str, slug: str) -> None:
    assert slugify_key(key) == slug


def _manifests() -> dict[str, ObjectManifest]:
    pinned = ObjectManifest(
        key="zarr/imaging",
        kind="icechunk",
        locator={"uri": "s3://lab/imaging.icechunk", "branch": "main", "region": None},
        policy=Policy(),
        state=STATE,
        pin=Pin(id="22620a34.73e341e966c83d38", ref="tether.22620a34.73e341e966c83d38"),
        captured_at="2026-10-09T00:00:00+00:00",
    )
    recorded = ObjectManifest(
        key="raw/plate1",
        kind="file",
        locator={"uri": "/data/raw/plate1"},
        policy=Policy(file="versioned", pin="record"),
        state={"type": "dir", "count": 2, "size": 10, "digest": "ab"},
        recoverable=False,
        origin="created",
    )
    return {m.key: m for m in (pinned, recorded)}


def test_manifest_hash_and_toml() -> None:
    """The manifest hash is over canonical JSON, not the TOML; the TOML is
    what a commit records, so a change to it rewrites committed files."""
    objects = _manifests()
    assert manifest_hash(objects) == (
        "5a9cd7634fdc0a82ebcf738b621e1d92345aed4d9f51b0953eb23716484eb439"
    )
    assert objects["zarr/imaging"].to_toml() == (
        'key = "zarr/imaging"\nkind = "icechunk"\nrecoverable = true\n'
        'captured_at = "2026-10-09T00:00:00+00:00"\n\n'
        '[locator]\nuri = "s3://lab/imaging.icechunk"\nbranch = "main"\n\n'
        '[policy]\nfile = "immutable"\npin = "native"\n\n'
        '[state]\nsnapshot_id = "1CECHNKREP0F1RSTCMT0"\nbranch = "main"\nn = 3\n\n'
        '[pin]\nid = "22620a34.73e341e966c83d38"\n'
        'ref = "tether.22620a34.73e341e966c83d38"\n'
    )
    assert objects["raw/plate1"].to_toml() == (
        'key = "raw/plate1"\nkind = "file"\nrecoverable = false\norigin = "created"\n\n'
        '[locator]\nuri = "/data/raw/plate1"\n\n'
        '[policy]\nfile = "versioned"\npin = "record"\n\n'
        '[state]\ntype = "dir"\ncount = 2\nsize = 10\ndigest = "ab"\n'
    )
    pinned, recorded = objects["zarr/imaging"], objects["raw/plate1"]
    assert ObjectManifest.from_toml(recorded.to_toml()) == recorded
    # `None` in a locator is dropped on write, so it reads back without it.
    back = ObjectManifest.from_toml(pinned.to_toml())
    assert back.locator == {"uri": "s3://lab/imaging.icechunk", "branch": "main"}
    assert (back.state, back.pin, back.captured_at) == (
        pinned.state,
        pinned.pin,
        pinned.captured_at,
    )


def test_repo_config_toml() -> None:
    config = RepoConfig(
        dataset_id="22620a34",
        vcs={"trunk": "main"},
        backends={"file": {"storage_options": {"region": "us-west-2"}}},
    )
    text = config.to_toml()
    assert text == (
        '[tether]\nversion = 5\n\n[dataset]\nid = "22620a34"\n\n'
        "[snapshot]\nauto = false\n\n[verify]\non_status = false\n\n"
        '[new]\nfork = "lazy"\n\n[defaults]\nfile = "immutable"\npin = "native"\n\n'
        '[vcs]\ntrunk = "main"\n\n'
        '[backends.file.storage_options]\nregion = "us-west-2"\n'
    )
    assert RepoConfig.from_toml(text) == config


def test_plan_digest() -> None:
    """Over the plan's JSON as it reads back (a tuple becomes a list), keys
    sorted, compact; a saved plan whose content no longer matches it is
    refused."""
    plan = Plan(
        command="commit",
        actions=[
            Action(
                op="pin",
                key="zarr/imaging",
                kind="icechunk",
                target="tether.22620a34.73e341e966c83d38",
                detail="new state",
                params={"state": STATE, "pin_id": "22620a34.73e341e966c83d38"},
            )
        ],
        context={
            "message": "Baseline",
            "states": {"zarr/imaging": STATE},
            "pair": ("a", 1),
        },
        notes=["raw/plate1: unchanged"],
        preconditions=[
            Precondition("manifest_hash", "ab" * 32, detail="the manifests changed"),
            Precondition(
                "ref_absent",
                None,
                key="zarr/imaging",
                params={"ref": "tether.ws.22620a34.feat-x"},
            ),
        ],
        created_at="2026-10-09T00:00:00+00:00",
        tether_version="0.1.0b6",
    )
    assert plan.digest() == (
        "d2dca8c5c8069e9b1638ddaa8834f068b1270c52d239ad0cd1e61adcbedccaa1"
    )


def test_file_listing_and_digest() -> None:
    """A listing escapes non-ASCII (`json.dumps` defaults), unlike canonical
    JSON; the directory digest is BLAKE2b-128 over the sorted (path, token)
    pairs, each NUL-terminated."""
    rows = {
        "sub/c.csv": ("e3b0c442", 12),
        "a.csv": ("9f86d081", 3),
        "é/ü.txt": ("2c26b46b", 0),
    }
    text = _dump_listing(rows)
    assert text == (
        '{"p":"a.csv","k":"9f86d081","s":3}\n'
        '{"p":"sub/c.csv","k":"e3b0c442","s":12}\n'
        '{"p":"\\u00e9/\\u00fc.txt","k":"2c26b46b","s":0}\n'
    )
    assert _load_listing(text) == rows
    token = _link_token("../elsewhere")
    assert token == (
        "symlink:b8543627c760485944d30231e661918e1e706e365a5c40124f37de2a17ea8048"
    )
    pairs = [
        ("sub/c.csv", "e3b0c442"),
        ("a.csv", "9f86d081"),
        ("é/ü.txt", "2c26b46b"),
        ("link", token),
    ]
    assert _digest_pairs(pairs) == "50d8ea564dd02e2f8930211045c6a666"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_directory_fingerprint(tmp_path: Path) -> None:
    """The walk end to end: nested files, a non-ASCII name, an empty file, a
    symlink to a file (read through), one to a directory and a dangling one
    (each recorded by its target, not followed), and the owner marker (not
    content)."""
    root = tmp_path / "tree"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.csv").write_bytes(b"a,b\n1,2\n")
    (root / "sub" / "c.csv").write_bytes(b"c\n")
    (root / "sub" / "deep" / "d.bin").write_bytes(bytes(range(256)))
    (root / "é.txt").write_bytes(b"accent\n")
    (root / "empty.txt").write_bytes(b"")
    os.symlink("a.csv", root / "link-to-a")
    os.symlink("sub", root / "link-to-sub")
    os.symlink("nowhere", root / "dangling")
    (root / ".tether-owner").write_text("22620a34\n", encoding="utf-8")

    backend = FileBackend()
    locator = {"uri": str(root)}
    state = backend.fingerprint(locator, None)
    assert state == {
        "type": "dir",
        "count": 8,
        "size": 281,
        "digest": "5ab26439559322b53fc2d541e7850724",
    }
    a_csv = "492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470"
    assert backend.listing(locator, state) == (
        f'{{"p":"a.csv","k":"{a_csv}","s":8}}\n'
        '{"p":"dangling","k":"symlink:'
        '20aeff0494e828d188c704e1f488a589b15ae01d11f6cb129f62129caa6cc543","s":0}\n'
        '{"p":"empty.txt","k":'
        '"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","s":0}\n'
        f'{{"p":"link-to-a","k":"{a_csv}","s":8}}\n'
        '{"p":"link-to-sub","k":"symlink:'
        'ddc6e2b224d0fd821669202258386936fc9ce2899e215eec6322b95f8dd96d6a","s":0}\n'
        '{"p":"sub/c.csv","k":'
        '"a3a5e715f0cc574a73c3f9bebb6bc24f32ffd5b67b387244c2c909da779a1478","s":2}\n'
        '{"p":"sub/deep/d.bin","k":'
        '"40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880","s":256}\n'
        '{"p":"\\u00e9.txt","k":'
        '"8f8df9963c9628741bfeeac7efb739164d0858fd03eb1950f385bb26512cef55","s":7}\n'
    )
    assert backend.fingerprint({"uri": str(root / "a.csv")}, None) == {
        "type": "file",
        "size": 8,
        "sha256": a_csv,
    }


def test_history_digest() -> None:
    """Order-independent: what a gc plan binds visible history to."""
    revs = ["b" * 40, "a" * 40, "0" * 40]
    assert _digest_revs(revs) == "d734a905b8bb2585"
    assert _digest_revs(list(reversed(revs))) == "d734a905b8bb2585"
