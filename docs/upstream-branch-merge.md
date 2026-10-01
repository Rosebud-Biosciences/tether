# Branch merge and promotion upstream: Icechunk and Lance

Checked **2026-10-01** against Icechunk's documentation and format, and the
`earth-mover/icechunk`, `lance-format/lance` and `lancedb/lancedb` trackers.
Upstream moves quickly; re-check the issues below before relying on any of
this.

The README's backend matrix gives Icechunk `Promote` but not `Merge`, and
Lance neither. This note records why, what upstream is doing about it, and
what would have to land before either cell changes.

## Summary

Neither library merges branches. Icechunk can fast-forward one branch to
another's snapshot, which is all tether's `PROMOTE` needs, and a diverged
base is refused. Lance cannot move a branch head at all. Each has an open
feature request and no implementation PR. Lance's request carries a written
design that is blocked on two format changes; Icechunk has a design only for
merging sibling snapshots, not branches.

## Icechunk

### What exists

- **Fast-forward.** A repository's branches share one set of snapshots and
  chunks, so pointing a branch at another branch's snapshot is a ref update
  that copies no data. `reset_branch(branch, snapshot_id,
  from_snapshot_id=...)` makes it a compare-and-swap, atomic on object
  stores only (`ROADMAP.md`, "Icechunk on local storage"). tether's
  `promote` is this call, after checking that the base's head is an
  ancestor of the fork's.
- **Rebase within one branch.** A session whose branch moved under it can
  `rebase` onto the new tip before it commits. Automatic resolution
  (`BasicConflictSolver`) covers chunk conflicts only; metadata and other
  conflicts are resolved by hand. The
  [Git users guide](https://icechunk.io/en/stable/guides/icechunk-for-git-users/)
  says branches cannot be merged or rebased onto each other.
- **Session `fork` / `merge`.** Distributed writers producing one commit
  from one base. It does not cross branches.

### Open issues

| Issue | Opened | Asks for | Status |
| --- | --- | --- | --- |
| [icechunk#1811](https://github.com/earth-mover/icechunk/issues/1811) "Support cherry-pick" | 2026-03-11 | landing several branches on `main`; motivated by inference written to one branch per independent region | open, no comments, no PR |
| [icechunk#2060](https://github.com/earth-mover/icechunk/issues/2060) "Allow merging of anonymous snapshots into a single Session" | 2026-04-09 | one commit from snapshots that distributed workers wrote with `Session.flush` | open; a maintainer sketched a design on 2026-07-16; no PR |
| [icechunk#1871](https://github.com/earth-mover/icechunk/issues/1871) "Provide a 'fast forward' rebase option" | 2026-03-19 | an "unsafe" session rebase that skips conflict detection | open, no PR |
| [icechunk#2309](https://github.com/earth-mover/icechunk/issues/2309) "Add branch protection to prevent accidental commits" | 2026-07-30 | refusing commits to protected branches, so only a release step advances `main` | open, no comments |

### Blockers

No maintainer has named one: none of these issues has a label, a milestone
or a PR. The format and the #2060 sketch imply three (tether's reading, not
upstream's):

- **No merge commits.** A snapshot has exactly one parent (`parent_offset`
  in the repo info file,
  [`repo.fbs`](https://github.com/earth-mover/icechunk/blob/main/icechunk-format/flatbuffers/repo.fbs)),
  so history is a tree. A merge would squash or replay; it cannot record
  two parents without a format change.
- **#2060 would not give tether `MERGE`.** Its sketch requires every
  snapshot to share a parent that is still the branch's tip, then merges
  their transaction logs, fails on conflicts, merges the manifests and
  commits one snapshot. A diverged fork fails both conditions: the base has
  moved past the fork point, and the fork is usually a chain of snapshots.
  Replaying a fork onto a moved base needs cherry-pick (#1811) or a merge
  that takes a merge base.
- **Conflict resolution is chunk-only.** A merge of forks that changed
  array metadata, shapes or attributes would refuse rather than resolve.

### What would change in tether

`MERGE` needs an upstream cherry-pick or branch merge. Until then the
refusal's advice stands: re-apply the writes on a fresh fork of the base,
or `reset_branch` the base if losing its newer snapshots is intended.

## Lance

### What exists

- **Branches, no merge.** A branch is a shallow clone under `tree/<name>/`
  with its own linear version history; its manifest reaches the parent's
  data files through `base_paths`. Nothing moves a branch head to a version
  on another branch: there is no merge, cherry-pick or fast-forward. Landing
  a fork means writing its rows onto the base branch again, which copies
  them.
- **LanceDB's remote merge.**
  [lancedb#3686](https://github.com/lancedb/lancedb/pull/3686) (merged
  2026-07-18) added `table.branches.diff()` and `merge()` against the
  LanceDB REST API (Cloud and Enterprise). There, "merge" means promoting
  columns added on a branch onto `main`, and nothing else; local tables
  raise `NotSupported` until lance#7263 lands. tether opens datasets with
  `lance.dataset` and never talks to that API.
- **A name to ignore.** `LanceDataset.merge` and `merge_insert` are a column
  join and an upsert, unrelated to branches.

### The request

[lance#7263](https://github.com/lance-format/lance/issues/7263) "Branch
merge and rebase", opened 2026-06-13 by a Lance maintainer, proposes:

1. read the source branch's transactions since the fork;
2. graft the source branch's root into the target's `base_paths`, so the
   merged data files are read where they are;
3. commit through the normal path, whose optimistic conflict resolution
   handles a target that moved.

Fast-forward and rebase fall out of the same primitive. It is open, with no
PR.

### Blockers

Three open issues stand in front of it; each links #7263 or is linked from
it.

| Issue | What is missing | Status |
| --- | --- | --- |
| [lance#7185](https://github.com/lance-format/lance/issues/7185) "Can not delete branches referenced by other branches" | A branch's storage path is its name (`tree/feature`), so a rebase cannot repoint `feature` at a replayed line, and a branch that other branches depend on cannot be deleted. Proposed: UUID-named branch directories, with a name-to-UUID map in branch metadata. | open; [lance#8403](https://github.com/lance-format/lance/pull/8403) attempted it and was closed on 2026-08-08 as "too complex to fix" |
| [lance#7514](https://github.com/lance-format/lance/issues/7514) "cleanup_old_versions does not protect data files referenced via base_paths from another dataset" | Cleanup keeps files that *descendant* branches of the same dataset reference. A merge into `main` makes an *ancestor* reference files in the fork's tree, which cleanup does not look for. | open; draft [lance#8246](https://github.com/lance-format/lance/pull/8246) registers clones on their source, which its author calls a format change needing a vote, or else a documented limitation |
| [lance#7515](https://github.com/lance-format/lance/issues/7515) "In-place materialize of base-referenced files" | Nothing copies base-referenced files into a dataset's own storage in place, so a branch that adopted another's files can never become independent of it. | open, no PR |

### What would change in tether

lance#7263 is what a Lance `PROMOTE` (and, with conflict handling, `MERGE`)
would call, but it is not enough alone. tether retires a landed fork's
branch once nothing pins it (`gc --prune-bookmarks`), and deleting a Lance
branch removes `tree/<name>/`. Under #7263 as proposed, `main` would read
data files from that directory, so pruning the fork would break `main`.
Until lance#7514 and lance#7515 land, a Lance promote would have to keep the
fork's branch for as long as `main` references it, or copy its files first.

## Re-checking

Title searches for `merge`, `rebase`, `cherry`, `fast-forward`, `promote`
and `branch` over open and closed issues in both trackers found everything
above; in Lance, `merge` also matches `merge_insert` work. PR searches for
the issue numbers, `cherry`, `graft` and `fast forward` found no
implementation of any of it. When a cell in the README's matrix changes,
update this note, the matrix, and the backends guide together.
