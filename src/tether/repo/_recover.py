"""`recover`: what a lost dataset left in its stores, and how to take it back."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tether.backends.base import Capability
from tether.manifest import (
    bookmark_slug,
    pin_dataset,
    working_ref_bookmark,
    working_ref_dataset,
    working_ref_generation,
)
from tether.repo._core import RepoCore
from tether.repo._reports import RecoveredObject, RecoveredRefs, RecoverReport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tether.repo import Repo


class RecoverOps(RepoCore):
    """`recover`: tether's refs in each store, by the dataset they carry."""

    def recover_report(self: Repo, keys: Sequence[str] | None = None) -> RecoverReport:
        """List tether's refs in each object's store, grouped by the dataset id
        they carry, and say what to run to take them back. Read-only.

        A dataset whose repository was lost leaves its refs in the stores:
        pins `tether.<dataset>.<hash>`, and per bookmark a working branch
        `tether.ws.<dataset>.<bookmark>` (with `.<n>` generations where a
        store made siblings). The report counts the pins and names the
        bookmarks under every dataset id it finds, this one's and any
        other's, from the same listings `gc` reads. `steps` are the commands
        that take them back: the id to set in `tether.toml` when the refs
        are another dataset's and nothing is pinned under this one yet; a
        `commit` on the trunk, which re-pins each upstream branch (a state
        pinned before reuses its tag); then per bookmark `new -b BOOKMARK
        TRUNK --adopt` and a `commit`.

        Args:
            keys: Only these objects (keys, or prefixes ending in `/`; see
                `select_keys`); default: every object.

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
                for pin_id in backend.list_pins(m.locator):
                    ds = pin_dataset(pin_id)
                    if ds is not None:
                        under(ds).pins.add(pin_id)
            if Capability.FORK in caps:
                for ref in backend.list_working_refs(m.locator):
                    ds, slug = working_ref_dataset(ref), working_ref_bookmark(ref)
                    if ds is not None and slug is not None:
                        under(ds).bookmarks.setdefault(slug, []).append(ref)
            for refs in found.namespaces.values():
                for branches in refs.bookmarks.values():
                    branches.sort(key=lambda ref: working_ref_generation(ref) or 1)
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
        found = report.datasets
        current, trunk = report.dataset_id, report.trunk
        if not found:
            return [
                "no tether refs in these stores: nothing to take back; a "
                f"`tether commit` on the trunk ({trunk}) pins the objects afresh"
            ]
        others = sorted(ds for ds in found if ds != current)
        elsewhere = (
            f"this dataset has pinned under {current} already, so its id can no "
            "longer change; take them back in a fresh repository: `tether init "
            f"--dataset-id {others[0] if len(others) == 1 else 'ID'}`, `tether "
            "add` the objects, then `tether recover` there"
        )
        steps: list[str] = []
        target: str | None = current if current in found else None
        if target is None and report.pinned:
            return [f"the refs are {', '.join(others)}'s, and {elsewhere}"]
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
        marks = self.vcs.bookmarks()
        for slug, branches in sorted(found[target].bookmarks.items()):
            if slug == bookmark_slug(trunk):
                steps.append(
                    f"{branches[-1]} is named for the trunk, whose working refs "
                    "are the upstream branches: on a bookmark, `tether restore "
                    f"KEY --at {branches[-1]}` copies it"
                )
                continue
            new = (
                f"tether new {slug} --adopt"
                if slug in marks
                else f"tether new -b {slug} {trunk} --adopt"
            )
            steps.append(f'{new} && tether commit -m "Recover {slug}"')
        if others and target == current:
            steps.append(
                f"the refs of {', '.join(others)} are "
                + ("another dataset's" if len(others) == 1 else "other datasets'")
                + " and are left as they are; if they were this one's, "
                + (
                    elsewhere
                    if report.pinned
                    else "set its id in tether.toml before anything is pinned"
                )
            )
        return steps
