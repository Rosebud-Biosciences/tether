"""`recover`: what a lost dataset left in its stores, and how to take it back."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING

from tether.backends.base import Capability
from tether.manifest import (
    bookmark_slug,
    owner_from_ref,
    pin_dataset,
    pin_id_of_ref,
    ref_for_pin,
    working_ref_bookmark,
    working_ref_dataset,
    working_ref_generation,
    working_ref_workspace,
)
from tether.repo._core import RepoCore
from tether.repo._reports import RecoveredObject, RecoveredRefs, RecoverReport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo

_ESCAPED = re.compile(r"-[0-9a-f]{6}$")
"""The digest `bookmark_slug` appends to a name it had to escape."""


class RecoverOps(RepoCore):
    """`recover`: tether's refs in each store, by the dataset they carry."""

    def recover_report(self: Repo, keys: Sequence[str] | None = None) -> RecoverReport:
        """List tether's refs in each object's store, grouped by the dataset id
        they carry, and say what to run to take them back. Read-only.

        A dataset whose repository was lost leaves its refs in the stores:
        pins `tether.<dataset>.<hash>`, and per bookmark a working branch
        `tether.ws.<dataset>.<bookmark>` (with `.<n>` generations where a
        store made siblings) -- or, from before bookmarks, per workspace
        `tether.ws.<dataset>.<workspace>.<key>` (`legacy`). The report counts
        the pins and names the bookmarks under every dataset id it finds,
        this one's and any other's, from the same listings `gc` reads.
        `steps` are the commands that take them back: `repair` first where a
        manifest names a pin its store lacks; the id to set in `tether.toml`
        when the refs are another dataset's and nothing is pinned under this
        one yet; a `commit` on the trunk, which re-pins each upstream branch
        (a state pinned before reuses its tag); then per bookmark `new
        BOOKMARK --adopt` (`new -b BOOKMARK TRUNK --adopt` where the VCS has
        no such bookmark) and a `commit`; and per legacy branch a `restore
        --at` that copies it.

        Args:
            keys: Only these objects (keys, or prefixes ending in `/`; see
                `select_keys`); default: every object. The dataset id
                applies to every object, so a report on some advises none.

        Raises:
            ConfigError: A selector matches no object.
        """
        selected = self.select_keys(keys)

        def listing(key: str) -> RecoveredObject:
            m = self.objects[key]
            backend = self.backend_for(m.kind)
            found = RecoveredObject(key, m.kind)
            caps = backend.capabilities
            if not caps & (Capability.PIN | Capability.FORK):
                found.holds_refs = False
                return found

            def under(ds: str) -> RecoveredRefs:
                return found.namespaces.setdefault(ds, RecoveredRefs(ds))

            if Capability.PIN in caps:
                listed: set[str] = set()
                for raw in backend.list_pins(m.locator):
                    ref = ref_for_pin(raw)
                    pin_id = pin_id_of_ref(ref)
                    ds = pin_dataset(pin_id) if pin_id is not None else None
                    if pin_id is None or ds is None:
                        if owner_from_ref(ref) is None:
                            found.unrecognized.append(ref)
                        continue
                    listed.add(pin_id)
                    under(ds).pins.add(pin_id)
                if m.pin is not None and m.pin.id not in listed:
                    found.missing_pin = m.pin.id
            if Capability.FORK in caps:
                for ref in backend.list_working_refs(m.locator):
                    ds, slug = working_ref_dataset(ref), working_ref_bookmark(ref)
                    # Legacy, removed at 0.1.0: per-workspace branches' workspace
                    ws = working_ref_workspace(ref)
                    if ds is not None and slug is not None:
                        under(ds).bookmarks.setdefault(slug, []).append(ref)
                    # Legacy, removed at 0.1.0: listing per-workspace branches
                    elif ds is not None and ws is not None:
                        under(ds).legacy.setdefault(ws, []).append(ref)
                    else:
                        found.unrecognized.append(ref)
            for refs in found.namespaces.values():
                for branches in refs.bookmarks.values():
                    branches.sort(key=lambda ref: working_ref_generation(ref) or 1)
                # Legacy, removed at 0.1.0: sorting per-workspace branches
                for branches in refs.legacy.values():
                    branches.sort()
            found.unrecognized.sort()
            return found

        results, errors = self._fanout_collect(listing, selected)
        report = RecoverReport(
            dataset_id=self.config.dataset_id,
            trunk=self.config.trunk,
            objects=[
                results[key]
                if key in results
                else RecoveredObject(
                    key, self.objects[key].kind, error=str(errors[key])
                )
                for key in selected
            ],
            scoped=keys is not None,
        )
        mine = report.datasets.get(report.dataset_id)
        report.pinned = any(m.pin is not None for m in self.objects.values()) or bool(
            mine is not None and mine.pins
        )
        report.steps = self._recover_steps(report)
        return report

    def _recover_steps(self, report: RecoverReport) -> list[str]:
        """What to run, in order, to take back what `report` found; sets
        `report.suggested_id`. Nothing is suggested while a selected store
        could not be listed: its refs may name another id or more
        bookmarks, and "nothing found" would steer to a fresh commit."""
        failed = [o.key for o in report.objects if o.error is not None]
        if failed:
            return [
                f"could not list the refs of {', '.join(failed)}: fix access to "
                "their stores and run `tether recover` again, or leave them out "
                "(`tether recover KEY...`); nothing is suggested from a partial "
                "listing"
            ]
        steps: list[str] = []
        missing = [o.key for o in report.objects if o.missing_pin is not None]
        if missing:
            steps.append(
                f"tether repair  (the manifests of {', '.join(missing)} name pins "
                "their stores lack; repair recreates them from the recorded "
                "states -- a `tether commit` does not, the states being unchanged)"
            )
        steps.extend(self._take_back_steps(report))
        unrecognized = sorted({r for o in report.objects for r in o.unrecognized})
        if unrecognized:
            steps.append(
                f"{', '.join(unrecognized)} look like tether's but carry no "
                "dataset id it can read, so nothing above takes them back: "
                "look at them before pinning afresh; on a bookmark, `tether "
                "restore KEY --at REF` copies a branch"
            )
        return steps

    def _take_back_steps(self, report: RecoverReport) -> list[str]:
        """The id, trunk, bookmark and legacy-branch steps of
        `_recover_steps`."""
        found = report.datasets
        current, trunk = report.dataset_id, report.trunk
        if not found:
            if any(o.unrecognized or o.missing_pin for o in report.objects):
                return []
            return [
                "no tether refs in these stores: nothing to take back; a "
                f"`tether commit` on the trunk ({trunk}) pins the objects afresh"
            ]
        others = sorted(ds for ds in found if ds != current)
        whole = (
            "the id applies to every object: run `tether recover` without keys "
            "before choosing it"
        )
        named = others[0] if len(others) == 1 and not report.scoped else "ID"
        elsewhere = (
            f"this dataset has pinned under {current} already, so its id can no "
            "longer change; take them back in a fresh repository: `tether init "
            f"--dataset-id {named}`, `tether add` the objects, then `tether "
            "recover` there" + (f" ({whole})" if report.scoped else "")
        )
        steps: list[str] = []
        target: str | None = current if current in found else None
        if target is None and report.pinned:
            return [f"the refs are {', '.join(others)}'s, and {elsewhere}"]
        if target is None and report.scoped:
            return [
                f"the refs are {', '.join(others)}'s, not {current}'s (nothing "
                f"is pinned under {current} yet, so the id can still change); "
                f"{whole}"
            ]
        if target is None and len(others) == 1:
            target = report.suggested_id = others[0]
            steps.append(
                f"set this dataset's id in tether.toml (nothing is pinned under "
                f'{current} yet):\n[dataset]\nid = "{target}"'
            )
        elif target is None:
            steps.append(
                f"the stores hold refs of {len(others)} datasets; set the id "
                f"that was this one's in tether.toml (nothing is pinned under "
                f"{current} yet), one of these under [dataset]:\n"
                + "\n".join(f'id = "{ds}"' for ds in others)
            )
        steps.append(
            ("" if self.workspace.bookmark == trunk else f"tether new {trunk} && ")
            + f'tether commit -m "Recover {trunk}"'
        )
        if target is None:
            steps.append(
                "then, per bookmark of that id: `tether new -b BOOKMARK "
                f"{trunk} --adopt` and `tether commit`"
            )
            return steps
        by_slug: dict[str, list[str]] = {}
        for name in self.vcs.bookmarks():
            by_slug.setdefault(bookmark_slug(name), []).append(name)
        for slug, branches in sorted(found[target].bookmarks.items()):
            if slug == bookmark_slug(trunk):
                steps.append(
                    f"{branches[-1]} is named for the trunk, whose working refs "
                    "are the upstream branches: on a bookmark, `tether restore "
                    f"KEY --at {branches[-1]}` copies it"
                )
                continue
            names = sorted(by_slug.get(slug, []))
            if len(names) == 1:
                steps.append(
                    f"tether new {shlex.quote(names[0])} --adopt && tether commit "
                    f'-m "Recover {names[0]}"'
                )
            elif names:
                steps.append(
                    f"{branches[-1]} is the branch of whichever of the bookmarks "
                    f"{', '.join(names)} it was (their names escape alike): "
                    "`tether new NAME --adopt` with that one, then `tether commit`"
                )
            elif _ESCAPED.search(slug):
                steps.append(
                    f"{branches[-1]} names bookmark {slug}, which looks escaped "
                    "(a name with `/`, `.` or spaces gets a digest of the "
                    "original appended); the original name cannot be recovered "
                    f"from the branch, and this takes it under {slug}:\n"
                    f"tether new -b {slug} {trunk} --adopt && tether commit -m "
                    f'"Recover {slug}"'
                )
            else:
                steps.append(
                    f"tether new -b {slug} {trunk} --adopt && tether commit -m "
                    f'"Recover {slug}"'
                )
        # Legacy, removed at 0.1.0: the `restore --at` step per legacy branch
        for o in report.objects:
            legacy = o.namespaces[target].legacy if target in o.namespaces else {}
            for ws, branches in sorted(legacy.items()):
                steps.extend(
                    f"{ref} is a legacy branch of workspace {ws} (named before "
                    "bookmarks) and may hold writes no commit pins: on a "
                    f"bookmark, `tether restore {o.key} --at {ref}` copies it"
                    for ref in branches
                )
        if others and target == current:
            if report.pinned:
                then = elsewhere
            elif report.scoped:
                then = whole
            else:
                then = "set its id in tether.toml before anything is pinned"
            steps.append(
                f"the refs of {', '.join(others)} are "
                + ("another dataset's" if len(others) == 1 else "other datasets'")
                + f" and are left as they are; if they were this one's, {then}"
            )
        return steps
