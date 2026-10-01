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
from tether.repo._reports import (
    RecoveredObject,
    RecoveredRefs,
    RecoverReport,
    RecoverStep,
)

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
        `steps` (each a quoted command, or none, and a note) take them back:
        first a `repair` of the objects whose manifest names a pin their
        store lacks (of the selected ones only, with `keys`); the id
        to set in `tether.toml` when the refs are another dataset's and
        nothing is pinned under this one yet; a `commit` on the trunk, which
        re-pins each upstream branch (a state pinned before reuses its tag);
        then per bookmark `new --adopt -- BOOKMARK` (`new -b BOOKMARK --adopt
        -- TRUNK` where the VCS has no such bookmark) and a `commit`; and per
        legacy branch, on a bookmark `recover-<workspace>` off the trunk, a
        `restore --at` that copies it, naming every object of a branch space
        together, and a `commit`. Each command ends its options with `--`:
        a key, and a git branch name, may start with `-`.

        Args:
            keys: Only these objects (keys, or prefixes ending in `/`; see
                `select_keys`); default: every object. The dataset id
                applies to every object, and a commit pins every object
                under it: a report on some advises no id and nothing that
                pins, and ends with `tether recover` on every object.

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

    def _recover_steps(self, report: RecoverReport) -> list[RecoverStep]:
        """What to run, in order, to take back what `report` found; sets
        `report.suggested_id`. Nothing is suggested while a selected store
        could not be listed: its refs may name another id or more
        bookmarks, and "nothing found" would steer to a fresh commit. A
        scoped report suggests nothing that pins anew: a commit pins every
        object under the current id, which then can no longer change, and
        an id found only in a store left out would be lost. Its `repair`
        names only the selected objects, and recreates only pins their
        manifests already name."""
        failed = [o.key for o in report.objects if o.error is not None]
        if failed:
            return [
                RecoverStep(
                    None,
                    f"could not list the refs of {', '.join(failed)}: fix access "
                    "to their stores and run `tether recover` again, or leave them "
                    "out (`tether recover KEY...`); nothing is suggested from a "
                    "partial listing",
                )
            ]
        steps: list[RecoverStep] = []
        missing = [o.key for o in report.objects if o.missing_pin is not None]
        if missing:
            steps.append(
                RecoverStep(
                    f"tether repair -- {' '.join(shlex.quote(k) for k in missing)}",
                    f"the manifests of {', '.join(missing)} name pins their stores "
                    "lack; repair recreates them from the recorded states -- a "
                    "`tether commit` does not, the states being unchanged",
                )
            )
        if report.scoped:
            steps.extend(self._scoped_steps(report))
        else:
            steps.extend(self._take_back_steps(report))
        unrecognized = sorted({r for o in report.objects for r in o.unrecognized})
        if unrecognized:
            steps.append(
                RecoverStep(
                    None,
                    f"{', '.join(unrecognized)} look like tether's but carry no "
                    "dataset id it can read, so no step here takes them back: look "
                    "at them before pinning afresh; on a bookmark, `tether restore "
                    "--at REF -- KEY` copies a branch",
                )
            )
        if report.scoped:
            steps.append(
                RecoverStep(
                    "tether recover",
                    "the dataset id applies to every object, and a commit pins "
                    "every object under it, after which the id can no longer "
                    "change: the steps that take these refs back come from "
                    "listing every store",
                )
            )
        return steps

    def _scoped_steps(self, report: RecoverReport) -> list[RecoverStep]:
        """What a scoped report says of the dataset ids it found: notes
        only, no id and nothing that pins (see `_recover_steps`)."""
        found = report.datasets
        current = report.dataset_id
        if not found:
            if any(o.unrecognized or o.missing_pin for o in report.objects):
                return []
            return [RecoverStep(None, "no tether refs in the selected stores")]
        others = sorted(ds for ds in found if ds != current)
        if not others:
            return []
        if report.pinned:
            then = (
                f"this dataset has pinned under {current} already, so its id can "
                "no longer change; if they were this one's, take them back in a "
                "fresh repository: `tether init --dataset-id ID`, `tether add` the "
                "objects, then `tether recover` there"
            )
        else:
            then = f"nothing is pinned under {current} yet, so the id can still change"
        return [
            RecoverStep(
                None,
                f"the refs of {', '.join(others)} are "
                + ("another dataset's" if len(others) == 1 else "other datasets'")
                + f", not {current}'s; {then}",
            )
        ]

    def _take_back_steps(self, report: RecoverReport) -> list[RecoverStep]:
        """The id, trunk, bookmark and legacy-branch steps of an unscoped
        `_recover_steps`."""
        q = shlex.quote
        found = report.datasets
        current, trunk = report.dataset_id, report.trunk
        recommit = (
            "" if self.workspace.bookmark == trunk else f"tether new -- {q(trunk)} && "
        ) + f"tether commit -m {q(f'Recover {trunk}')}"
        if not found:
            if any(o.unrecognized or o.missing_pin for o in report.objects):
                return []
            return [
                RecoverStep(
                    recommit,
                    "no tether refs in these stores: nothing to take back; a "
                    f"commit on the trunk ({trunk}) pins the objects afresh",
                )
            ]
        others = sorted(ds for ds in found if ds != current)
        named = others[0] if len(others) == 1 else "ID"
        elsewhere = (
            f"this dataset has pinned under {current} already, so its id can no "
            "longer change; take them back in a fresh repository: `tether init "
            f"--dataset-id {q(named)}`, `tether add` the objects, then `tether "
            "recover` there"
        )
        steps: list[RecoverStep] = []
        target: str | None = current if current in found else None
        if target is None and report.pinned:
            return [
                RecoverStep(
                    None, f"the refs are {', '.join(others)}'s, and {elsewhere}"
                )
            ]
        if target is None and len(others) == 1:
            target = report.suggested_id = others[0]
            steps.append(
                RecoverStep(
                    None,
                    "set this dataset's id in tether.toml (nothing is pinned "
                    f'under {current} yet):\n[dataset]\nid = "{target}"',
                )
            )
        elif target is None:
            steps.append(
                RecoverStep(
                    None,
                    f"the stores hold refs of {len(others)} datasets; set the id "
                    f"that was this one's in tether.toml (nothing is pinned under "
                    f"{current} yet), one of these under [dataset]:\n"
                    + "\n".join(f'id = "{ds}"' for ds in others),
                )
            )
        steps.append(
            RecoverStep(
                recommit,
                f"pins each object's upstream branch on the trunk ({trunk}); a "
                "state pinned before reuses its tag",
            )
        )
        if target is None:
            steps.append(
                RecoverStep(
                    "tether recover",
                    "with the id set, the steps that take back its bookmarks: "
                    f"`tether new -b BOOKMARK --adopt -- {q(trunk)}`, then `tether "
                    "commit`, for each",
                )
            )
            return steps
        known = list(self.vcs.bookmarks())
        by_slug: dict[str, list[str]] = {}
        for name in known:
            by_slug.setdefault(bookmark_slug(name), []).append(name)
        adopts = (
            "takes its branches as they are, uncommitted writes included, and pins them"
        )
        for slug, branches in sorted(found[target].bookmarks.items()):
            branch = branches[-1]
            if slug == bookmark_slug(trunk):
                steps.append(
                    RecoverStep(
                        None,
                        f"{branch} is named for the trunk, whose working refs are "
                        "the upstream branches: on a bookmark, `tether restore "
                        f"--at {q(branch)} -- KEY` copies it",
                    )
                )
                continue
            names = sorted(by_slug.get(slug, []))
            if len(names) == 1:
                steps.append(
                    RecoverStep(
                        f"tether new --adopt -- {q(names[0])} && tether commit -m "
                        + q(f"Recover {names[0]}"),
                        f"bookmark {names[0]}: {adopts}",
                    )
                )
                continue
            if names:
                steps.append(
                    RecoverStep(
                        None,
                        f"{branch} is the branch of whichever of the bookmarks "
                        f"{', '.join(names)} it was (their names escape alike): "
                        "`tether new --adopt -- NAME` with that one, then `tether "
                        "commit`",
                    )
                )
                continue
            if _ESCAPED.search(slug):
                note = (
                    f"{branch} names bookmark {slug}, which looks escaped (a name "
                    "with `/`, `.` or spaces gets a digest of the original "
                    "appended); the original name cannot be recovered from the "
                    f"branch, and this takes it under {slug}: it {adopts}"
                )
            else:
                note = f"bookmark {slug}, made afresh off the trunk: {adopts}"
            known.append(slug)
            steps.append(
                RecoverStep(
                    f"tether new -b {q(slug)} --adopt -- {q(trunk)} && tether commit "
                    f"-m {q(f'Recover {slug}')}",
                    note,
                )
            )
        # Legacy, removed at 0.1.0: the `restore --at` steps for legacy branches
        steps.extend(self._legacy_restore_steps(report, target, known))
        if others and target == current:
            if report.pinned:
                then = elsewhere
            else:
                then = "set its id in tether.toml before anything is pinned"
            steps.append(
                RecoverStep(
                    None,
                    f"the refs of {', '.join(others)} are "
                    + ("another dataset's" if len(others) == 1 else "other datasets'")
                    + f" and are left as they are; if they were this one's, {then}",
                )
            )
        return steps

    # Legacy, removed at 0.1.0: the `restore --at` steps for legacy branches
    def _legacy_restore_steps(
        self, report: RecoverReport, target: str, bookmarks: Sequence[str]
    ) -> list[RecoverStep]:
        """A `restore --at` per legacy branch, naming the objects of its native
        branch space (one `(kind, branch_scope)`) together: `restore` refuses
        some of them alone, the branch being theirs as one. Each goes onto a
        bookmark `recover-<workspace>`, made off the trunk by the first step
        of its workspace unless `bookmarks` (the VCS's, and those the steps
        before make) has it, and joined by the rest: `restore` refuses the
        trunk. Each commits what it restored, since a `new` back onto the
        bookmark resets a branch whose restore is not committed."""
        q = shlex.quote
        groups: dict[tuple[str, str, str], list[str]] = {}
        workspace_of: dict[str, str] = {}
        for o in report.objects:
            legacy = o.namespaces[target].legacy if target in o.namespaces else {}
            if not legacy:
                continue
            m = self.objects[o.key]
            scope = self.backend_for(m.kind).branch_scope(m.locator)
            for ws, branches in legacy.items():
                for ref in branches:
                    groups.setdefault((m.kind, scope, ref), []).append(o.key)
                    workspace_of[ref] = ws
        made = set(bookmarks)
        steps: list[RecoverStep] = []
        for (_kind, _scope, ref), keys in sorted(
            groups.items(), key=lambda item: (item[1], item[0][2])
        ):
            ws = workspace_of[ref]
            bookmark = f"recover-{ws}"
            start = (
                f"tether new -- {q(bookmark)}"
                if bookmark in made
                else f"tether new -b {q(bookmark)} -- {q(report.trunk)}"
            )
            made.add(bookmark)
            together = (
                f"; {', '.join(keys)} share its branch space, so they are "
                "restored together"
                if len(keys) > 1
                else ""
            )
            steps.append(
                RecoverStep(
                    f"{start} && tether restore --at {q(ref)} -- "
                    + " ".join(q(k) for k in keys)
                    + f" && tether commit -m {q(f'Recover {ref}')}",
                    f"{ref} is a legacy branch of workspace {ws} (named before "
                    "bookmarks) and may hold writes no commit pins: this copies it "
                    f"onto bookmark {bookmark} and pins it{together}",
                )
            )
        return steps
