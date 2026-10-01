# Road to 0.1.0

What has to happen between the 0.1.0 betas and the first release that is
not one. Four lists: what still blocks a correct 0.1.0, what has only ever
run against a stand-in and needs a real resource under it, which tests are
missing, and what is experimental today and has to graduate or be cut.
Everything here is a decision or a test, not a feature.

One definition runs through all of it. **Stable** means the full lifecycle
-- register, commit, fork, write, commit, promote, gc -- is *exercised in
CI* against the real system. **Experimental** means it has run against a
fake, a local stand-in, or not at all. By that definition `file` and
`icechunk` are stable on local storage, which is what CI runs; their S3, GCS
and Azure paths are **not yet cloud-tested** and are labelled so in the
README and the backends guide until section 2's runs happen. `tether
backends` prints each kind's maturity, `add` prints a note for an
experimental kind, and `--create` and `gc --delete-stores` print theirs.

## 1. Correctness blockers

The beta-exit work (0.1.0b4) closed the first review's findings, and then
the second review's:

- **S1.** `secrets.toml`, `workspace.toml` and `ops.jsonl` are refused while
  the VCS tracks them, at `find` and at every refresh that sees a changed
  `workspace.toml` (each `open`, each writing command); git 2.38 is
  enforced.
- **D1.** `gc` never releases a pin id any manifest names, and local paths
  are resolved fully (symlinks, trailing slashes, `file://`).
- **D2.** `promote` compares fork heads with the bookmark's commit, not the
  working tree.
- **D3.** `undo ID` reverts only the fields still holding what the operation
  left, and refuses a `new` whose bookmark the checkout still works on.
- **D4.** Lance's conditional fork never deletes or replaces a branch it did
  not check (a small window remains; below).
- **D8.** Saved plans are format 3, with a digest binding actions and
  context to their preconditions.
- **U1-U5.** Format version 5 and one migration carry a 0.1.0b3 dataset's
  identities, listings, Lance, Neon and directory states, and DuckLake paths
  forward; `gc` and `verify` skip manifests of removed kinds; 0.1.0b3
  refuses a version 5 dataset.
- **jj template aliases.** tether's templates call keywords as methods, and
  the hostile-config fixture aliases them.

What remains:

| Blocker | What is missing | What settles it |
| --- | --- | --- |
| **Real-service runs** for `neon`, `dolt`, and S3 through SeaweedFS | Neon has now run by hand against a real project (section 2, "Recorded runs"), which found the next two blockers; its `promote` refusal, a 423 answer, protected pins, rate limits and the cost of a branch per pinning commit are still unobserved, and nothing runs in CI. Dolt has run only against fakes. The SeaweedFS-backed S3 test skips without `weed` on `PATH`, so CI has never run it. | The rest of the rows in section 2, once each, with the result recorded; `weed` in a CI image; a CI row where a credential or a container can live. |
| **`gc` cannot delete a Neon branch** | `plan_gc` reads no head for a `BRANCH_IS_STORAGE` branch ("branch is storage; deleting reclaims its data"), so a `--force-prune` delete carries `head: None`. `apply_gc`'s preflight (`_require_head`) takes that to mean "the plan could not read this head" and raises `StalePlanError` whenever the head *can* be read -- for Neon, always. `gc --prune-bookmarks --force-prune` therefore never deletes a retired bookmark's Neon branch, and fails before deleting anything else. The sandbox worked around it by deleting the branch through Neon's API before `gc`. | Record the head at plan time for storage branches too (one API call), or let a forced storage-branch delete skip the head check; a conformance test with a `BRANCH_IS_STORAGE` backend. |
| **`new` against a branch being written** | Fixed for joining, unreleased (after 0.1.0b5): `--adopt` now binds to the branch existing and being the newest generation, and a `--shared` join to it still building on the bookmark's pin, neither to its head. Before, a branch a running service writes moved between plan and apply -- a preview's Dagster daemon commits to its Neon branch every second or two, and Neon's content state includes `commit_xid` -- and every attempt failed with "moved since the plan was made"; a CI job saved a live preview's addresses from its first run instead. A reset (`--discard`) and `reuse` still bind to the head, which they depend on. | A release, then a live run: `new pr<N> --adopt` on a running preview, which lets the sandbox's `tether-fork.sh` drop its saved-addresses artifact. |
| **A writable Lance handle's address** | `open --json` prints a Lance handle as `<uri>#<branch>@v<N>` whether or not it is writable, the same form as a pinned read, so a consumer that reads `@vN` as a pin opens a writable fork read-only. Only the `read_only` field tells them apart. | Print a writable handle as `<uri>#<branch>` (its version is the open-time head, not a pin), or document that `read_only`, not the address, decides. |
| **S3 Tables needs SigV4** | pyiceberg reaches the S3 Tables Iceberg REST catalog only with `pyiceberg[rest-sigv4]` (boto3). The `iceberg` extra installs `pyiceberg` alone, so a dataset on S3 Tables fails at its first catalog call. | Add `rest-sigv4` to the `iceberg` extra, or name it in the backends guide's Iceberg section. |
| **Cloud runs** for `file` and `icechunk` on S3, GCS and Azure | The object-store code paths (obstore client options, versioned objects, `s3_storage` with per-object credentials, `delete_store` on a prefix) have run against in-memory stores and S3-compatible stand-ins only. | The S3, credentials, GCS and Azure rows in section 2. Until then the labels say "not yet cloud-tested". |
| **Lance's conditional fork** | Onto an existing branch, the head is compared and the branch then deleted and created again: a peer's branch created in that window can be replaced. A fork expecting "absent" is atomic. | A conditional branch update in Lance, then `CONDITIONAL_REF`; until then the window is documented and Lance stays stable. |
| **Icechunk on local storage** | `reset_branch(from_snapshot_id=)` and `create_branch` are atomic only on object stores (`CONDITIONAL_REF` on `s3://` only). On a local or NFS-shared filesystem, racing processes can each win. | A lock tether takes around local-storage moves, or a refusal of `--shared` there; the docs say so meanwhile. |
| **jj repo-level aliases** | tether replaces the user config layer, but jj still loads repo- and workspace-level config. A repo-level alias of `if()`, of a lambda parameter, or of `files()` still changes what tether reads. | Refuse such aliases after `jj config list --repo`, or an upstream jj flag that skips repo config. |
| **Native handles across `new`** | Every `open` follows the current bookmark, but a handle opened before another process's `new -b` keeps writing to its old branch. | A handle that checks the workspace's bookmark before it writes, or the documented "reopen after `new`" stays. |
| **Windows** | Writing commands are refused where `fcntl` is missing, and a default `open` is read-only; the README says so. Nothing has run on Windows at all. | A decision: implement the checkout and repository locks on `msvcrt`, or ship 0.1.0 with writes refused. Either way a Windows CI row (section 3). |
| **jj version range** | The minimum is jj 0.43.0 (the version tether's tests run against locally); CI installs 0.45.1 only. Nothing between or beyond has run. | The version-matrix row in section 3. |
| **git `promote`/`merge` under a detached `HEAD`** | With the default `ref = HEAD` the git backend moves the checked-out branch, and refuses a detached `HEAD`, which a colocated jj checkout always has. Documented; not fixed. | Decide whether to merge without a checkout (`merge-tree --write-tree`, `commit-tree`, a conditional `update-ref`) before 0.1.0, or keep the documented refusal. |
| **A teardown command** | Removing tether from a repository is a manual recipe in the Troubleshooting page: list and delete every `tether.<id>.*` ref per store. | A command, or the recipe stays and is exercised once by hand against every stable backend. |

## 2. Test against actual resources

Each line names the resource, what to run, and what would move as a result.
The engine tests and conformance suites cover the logic; these cover the
service. Run them once by hand before the release and, where a credential
can live in CI, keep them running.

| Resource | What to run | What it decides |
| --- | --- | --- |
| **S3 bucket** (any region; versioning on for one prefix) | `file` prefix and versioned-object objects: `add`, `commit`, `status --snapshot`, `verify`, `open --rev`, drift after an overwrite, `allow_http` and the other allowlisted `storage_options`. `icechunk` repository at `s3://`: the story of section 2 of the worked examples (fork, write, promote, gc). `add --create` / `gc --delete-stores` on an `s3://` prefix -- the `delete_store` arm has only run against an obstore in-memory store. | `file`'s Addressable tier for versioned objects is real; Icechunk-on-S3 is real; both drop the "not yet cloud-tested" label; store-lifecycle graduation criterion 1. |
| **AWS credentials via `secrets.toml`** (a `profile`, a `role_arn`, literal keys, an `endpoint_url` to a SeaweedFS) | Two Icechunk objects in two accounts pinned by one `commit` (the configuration guide's "two identities" section). `tether open` and `verify` with `region` overrides. A `Repo` held open past the assumed role's expiry (the credentials refresh five minutes before it). | The per-object credential layer is real (it is core today; the reviews asked twice whether it should be). |
| **GCS and Azure containers** | `file` objects and prefixes through obstore (`gs://`, `az://`); a versioned object on each. | Whether the `file` backend's object-store claims hold beyond S3, or the docs narrow to S3. |
| **Neon project** (Free for the default unprotected pins, which still cap a project at 10 branches; a paid plan, which allows a few protected branches, for `protected_pins = true`) | The `neon` lifecycle against the control plane: `add`, `commit` (a branch pin, protected when asked), `new` + writable open (branch), `promote` refusal path, `gc --prune-bookmarks` keeping a `BRANCH_IS_STORAGE` branch, `--force-prune` deleting one, `verify` with a suspended compute, a 423 answer during a pin. The lineage / xid caveat in the caveats guide, observed. | `neon` stays experimental at 0.1.0 (below); this run decides what the note says. |
| **Iceberg catalog** (a REST catalog -- Lakekeeper or Polaris in a container -- and one object-store warehouse) | The `iceberg` lifecycle: snapshot pins, branch forks, `promote` fast-forward, `log`, `add --pick`, a table with no snapshot yet, retention expiring a recorded state. pyiceberg 0.11 or newer. | `iceberg` graduates; the `RETENTION_BOUND` claim is observed rather than asserted. |
| **Dolt server** (container, MySQL protocol) | The `dolt` lifecycle including a conflicting `merge` (it must surface as `MergeConflict`), `HASHOF` pins, and a server whose `[uris]` entry is missing (no password may be sent). | `dolt` graduates. |
| **DuckLake catalog** (DuckDB with a Postgres or SQLite catalog and an object-store `data_path`) | Addressable reads by snapshot id; `log`; retention; a relative `metadata` path registered from another directory. | `ducklake` graduates. |
| **Postgres** (already in CI through `pytest-postgresql`) | Nothing new: the registry publish/import path runs against it. Run once against a managed Postgres (RDS, Neon) to catch permission and extension differences. | Registry graduation, together with a schema decision (below). |
| **A hosted git remote and a hosted jj remote** (GitHub; a jj-capable forge or a bare repository over SSH) | Two clones of one dataset: the two-clone scenario in `tests/test_store_lifecycle.py` against a real remote, including a `forget-workspace` on one side; `gc` in each clone before and after fetching (`keep-pin` until fetched, `--release-foreign` after); a bookmark pushed from one side and dropped on the other. `tether abandon` under `git` with a remote-tracking branch. A `git` object whose `remote` is configured, pinned from a clone. | Store-lifecycle graduation criterion 2; whether "gc only knows what this clone has fetched" needs more than documentation. |
| **A large local tree** (10^6 files) and **a large prefix** | `status` first and second fingerprint; `commit` with a listing; `diff --content`. Numbers into the performance guide. | The performance guide's claims are measured, not estimated. |

### Recorded runs

**The lab-platform sandbox**: one AWS account, with S3, S3 Tables and Neon;
tether 0.1.0b4 and 0.1.0b5; 2026-09-23 to 2026-09-25. A web app's dataset
had nine objects: Icechunk, Iceberg (S3 Tables' REST catalog), Lance, Delta
and a `file` prefix, plus four databases in one Neon project. The dataset was
pinned on `main`, then forked per pull request by CI
(`new -b pr<N> --eager`). Each fork was written by that PR's preview services
and retired with `gc --prune-bookmarks --force-prune`. `promote` did not run:
the app discards preview forks rather than landing them.

- **Worked:**
  - the baseline `commit`: Icechunk and Lance tags; Iceberg, Delta and Neon
    by record; the `file` prefix;
  - `new --eager` forking every forkable store;
  - writes on the forks through the stores' own libraries: Iceberg appends
    on a table branch, Icechunk snapshots, Lance versions on a branch;
  - `status --snapshot` on `main` staying `clean` while the forks moved;
  - `open --writable` starting a Neon compute for the fork;
  - a `--discard` reset while nothing was writing;
  - `gc` releasing the Iceberg, Icechunk and Lance branches.
- **Found:**
  - the `gc`-on-Neon, `new`-on-a-written-branch, Lance-address and S3 Tables
    rows in section 1;
  - `secrets.toml` replacing the committed Iceberg catalog table instead of
    merging into it (fixed in 0.1.0b5).
- **Observed:**
  - an Iceberg table needs a snapshot before it can be pinned;
  - deleting a Lance branch deletes its keys (`_refs/branches/<name>.json`
    and `tree/<name>/`), so a role that may not delete under the store cannot
    release a Lance fork. The backends guide should name that grant.

## 3. Tests to add

Rows that need no external resource, only time. Each becomes a job or a
fixture in `tests/`.

| Test | What it runs | What it decides |
| --- | --- | --- |
| **jj version matrix with a hostile user config** | The core loop, `gc`, `drop` and `undo` under jj 0.43.0 (the minimum), the CI pin (0.45.1) and the newest release, each with the hostile-config fixture from `tests/conftest.py` (colour forced on, `all()` and template keywords aliased, auto-tracking off, `log.showSignature`), and once more with those aliases at repo level. | Whether 0.43.0 stays the minimum, and how far the repo-level alias limit reaches. |
| **A hostile clone, end to end** | One fixture repository whose manifests try everything the trust boundary refuses: a relative or in-checkout git `path`, a URL `remote`, a committed `[import] query`, `git_path`, an endpoint in `storage_options`, keys with `..` and `\`, a dataset id reused from another dataset naming the same store. Every command runs against it and must refuse without contacting anything it should not. | The security-model section of the configuration guide is a test, not a promise. |
| **Multi-checkout interleavings** | Two jj workspaces (and two git worktrees) of one dataset stepping through `commit` vs `gc`, `new --shared` vs the first writable `open`, `drop` vs `new` on the dropped bookmark, `restore` vs a `--shared` peer's write, `promote` vs a commit on the trunk, in every order the locks allow. | The repository lock and the `expected`-head moves hold under interleaving, not only in the two races the review reproduced. |
| **Windows** | The read-only commands (`status`, `verify`, `diff`, `log`, `ops`, `gc --dry-run`) on a Windows runner, and the refusal message for a writing one; if the lock is implemented, the full suite. | The Windows decision in section 1. |
| **Every backend through the full conformance suite** | The `file` backend with its real capabilities (`DIFF`, `CREATE`, `ADDRESSABLE` for a versioned object), not the `FINGERPRINT`-only override the in-repo harnesses use today; `delete_store` refusing a non-empty store for every `CREATE` backend. | The suite's claims cover what ships. |

## 4. Experimental today; graduate or cut before 0.1.0

| Feature | Where | Graduates when | The call for 0.1.0 |
| --- | --- | --- | --- |
| **`iceberg`** | `tether.experimental.backends.iceberg` | The 0.1.0b4 fixes (empty tables, pyiceberg 0.11) are in, and it has run by hand against S3 Tables' REST catalog (section 2's "Recorded runs"; the SigV4 extra in section 1); what is left is the catalog row in section 2 as a CI job (a REST catalog in a container) and the conformance suite passing against it. Then move the module to `tether/backends/`, set `MATURITY = "stable"`, update the backends guide and README. | **Next to graduate**, after its CI row. |
| **`ducklake`** | `tether.experimental.backends.ducklake` | The 0.1.0b4 fix (absolute `metadata` path) is in; what is left is the section 2 row as a CI job (DuckDB with the Postgres catalog `pytest-postgresql` already provides). | **Next to graduate**, after its CI row. |
| **`dolt`** | `tether.experimental.backends.dolt` | The 0.1.0b4 fixes (per-server credentials, merges inside a transaction) are in; what is left is the section 2 row against `dolt sql-server` in a container, as a CI job, and the conformance suite against it. | Graduates **after a real-server run**; otherwise ships experimental. |
| **`lakefs`** | removed in 0.1.0b4 | -- | **Cut.** Every fork failed (lakeFS branch ids refuse the dots in `tether.ws.*` names), and `promote` booked lakeFS's merge commit as a fast-forward; redesigning ref naming for one experimental kind was not worth it. A dataset whose history names a lakeFS object still works: `gc` and `verify --all-history` skip those manifests with a note, and their pins still count as references. |
| **`neon`** | `tether.experimental.backends.neon` | A live run of the section 2 row (partly done, section 2's "Recorded runs"; the `gc` and `new` blockers in section 1 come first), and a cost story users accept: one branch per pinning commit, unprotected by default, protected pins on paid plans only. | **Stays experimental** at 0.1.0, with the note. |
| **Registry** (`export`, `publish`, `import`; `tether.experimental.registry`) | `tether.experimental.registry` | The export schema is declared frozen (a `schema_version` in the bundle and a documented compatibility promise), one round trip has run against a managed Postgres, and `import --sync` has been used on a real dataset. Move to `tether.registry`. | **Stays experimental** past 0.1.0 with a stated horizon. It does not block the release: nothing else depends on it. |
| **Store lifecycle** (`add --create`, `Repo.create`, `gc --delete-stores`, `--store`; `tether.experimental.lifecycle`) | `tether.experimental.lifecycle`; `Capability.CREATE` in `tether.backends.base` | The five criteria in the module docstring: S3 `delete_store` against a real bucket; the two-clone scenario against a real remote; one release cycle with no data-loss report; a second Forkable backend with `CREATE`; a decision on the touched index. Move the module to `tether/repo/_lifecycle.py`, drop the notes, re-home the CHANGELOG entry. | **Stays experimental** at 0.1.0. `Capability.CREATE` stays declared (it is a per-backend fact with conformance coverage). |
| **Per-URI and per-object credentials** (`secrets.toml` `[uris.*]` / `[objects.*]`; `ObjectBackend.configure_secrets` / `secrets_for`) | core (`tether.backends.base`, `Repo.backend_for`); the reviews asked twice whether it belongs in experimental | The two-identity run in section 2 has happened against real accounts (a `profile`, a `role_arn`, literal keys, an `endpoint_url`) for Icechunk and `file`, and no second resolution order was needed. Then it is simply core, and the configuration guide's "advanced" section is the contract. | Moves to `tether.experimental.credentials` behind `configure_secrets` if the run finds a second order is needed; the kind-level `[backends.<kind>]` secrets stay core either way. Nothing users write in `secrets.toml` changes. |
| **The alpha upgrade path** (`tether.upgrade`) | `tether.upgrade` | Does not graduate: it is *removed* at 0.1.0 as announced. The removal checklist is below. | -- |
| **`tether abandon`** | core (`Repo.abandon`) | Decided: it stays as the surgical form -- commits off a bookmark you are keeping, `--gc` to release their pins in one action -- while `drop` takes the whole line. The remaining question is only whether it behaves under `git` with remote-tracking branches (the two-clone and remote tests above). | Kept; documented next to `drop` as the pair they are. |

### Removing the upgrade path at 0.1.0

Before removal: publish the last beta, confirm `tether upgrade` from every
alpha format and from 0.1.0b3 against it once more, and write the "install
`tether-vcs==<last beta>`, upgrade, reinstall" message into the open-time
error (`LAST_BETA_WITH_UPGRADE` names it). Then remove, in one revision.
Outside `tether.upgrade` and `tests/upgrade/` (both go wholesale), every
legacy-only site in `src/`, `tests/` and `user_guide/` carries the marker
`Legacy, removed at 0.1.0` (a `#` comment in code, an HTML comment in the
guide): `rg 'Legacy, removed at 0.1.0'` lists them, and none may remain.

- the `tether.upgrade` package and `tests/upgrade/`, the `upgrade` CLI
  command, and the thin `Repo.plan_upgrade` / `apply_upgrade` / `upgrade`
  delegates (with the `config_version` precondition kind only upgrade plans
  carry, and `key_digest6`, which only its ref renames call);
- `Repo.find(allow_outdated=)` and the `allow_outdated` constructor argument,
  which exist only so `upgrade` can open an older dataset;
- `ObjectBackend.rename_pin` and `rename_working_ref` (and the Neon
  override), which only the ref-renaming migration calls;
- `working_ref_workspace` and the legacy per-workspace working-ref parsing
  (`gc --prune-bookmarks`'s "legacy branch" arm), the `UpgradeReport`
  re-export, and the Migrations section of `great-docs.yml`. Once unparsed,
  a per-workspace name would read as a bookmark slug with a dot in it, which
  `bookmark_slug` never makes: refuse those as unrecognized;
- `recover`'s listing of legacy per-workspace branches: `RecoveredRefs.legacy`,
  its `legacy` JSON key (a documented output change, for the changelog), its
  `restore --at` step and CLI lines, the half of its test that covers them,
  and the recovery step in the troubleshooting guide;
- the `ref_head` alternative for an `adopt` in
  `REQUIRED_ACTION_PRECONDITIONS["new"]` (plans saved by 0.1.0b5),
  `_require_adopted_present`, which `ref_present` makes redundant then, and
  the two tests that build b5-shaped plans by hand;
- the refusals of plan formats 1 and 2 (`_UNBOUND_FORMATS`): they collapse
  into the one "unsupported plan format" refusal;
- seeding the pin-ownership index (`tether-pinned.jsonl`) from the op logs
  (`Repo._seed_pinned`), for clones made before the index: **decide** then
  whether to move it into the last beta's `upgrade` or keep it. Kept, nothing
  changes; moved, a clone that skipped that beta keeps every pin it made as
  foreign until `gc --release-foreign`, and the tests that unlink the index
  to exercise the seed go too;
- the `upgrade` mentions in the guide, rewritten for the open-time message:
  the CLI guide's synopsis line and `upgrade` section; the troubleshooting
  rows for `ConfigError` and `tether.toml is version 4`; the configuration
  guide's version, dataset-id and `write`-policy lines; the DuckLake
  locator row; the `rename_pin` row of the extending guide; the planned
  commands in `great-docs.yml` and the README; and the docstrings and
  `--prune-bookmarks` help that name legacy branches;
- freeze `CONFIG_VERSION` for 0.1.x.

Not part of the removal, though they mention older versions: the lenient
`[dataset] id` parse of a version 1 `tether.toml` (so the open-time message,
not a parse error, meets an alpha dataset); skipping history manifests of
removed kinds (lakeFS) and Iceberg's volatile `metadata_location`, since
history keeps those manifests; `upgrade` in the op log's `NOT_UNDOABLE`, its
summary, and `vcs_drift` following its `rewritten_commits`, since op logs
keep those entries; and the `accepts_expected` shim for third-party backends
without the `expected` keyword, a backend-author decision of its own.

## 5. Before tagging 0.1.0

- Every backend the README matrix lists is either stable or marked
  *(experimental)* in the row; the matrix, the backends guide, and `tether
  backends` agree, and "stable" means exercised in CI everywhere it appears.
  `file` and `icechunk` either have their cloud rows run or keep the "not
  yet cloud-tested" note.
- The blockers in section 1 are each closed or decided, and the tests in
  section 3 run in CI.
- `tether.upgrade` is gone by the checklist above; `CONFIG_VERSION` is
  frozen for 0.1.x; a dataset from the last beta opens without `upgrade`.
- The performance guide's numbers come from section 2's runs.
- The changelog's `0.1.0` entry lists what graduated, what stayed
  experimental, and what was removed, in those words.
