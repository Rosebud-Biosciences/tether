# Rust for tether: where the time goes, and a plan

Measured **2026-10-09** on tether 0.1.0b6 and 0.1.0b7, Python 3.13, git 2.54
and jj 0.45.1, on an Apple Silicon laptop. `benchmarks/run.py` reproduces the
numbers below (the native scanner's after `uv sync --group native`); rerun it
before relying on them.

The question was whether to rewrite tether, or its core, in Rust for speed
and memory safety. This note records what the measurements say, what any
Rust design would have to respect, and the plan that follows from both.

## Summary

Not now, and not wholesale. A local command spends most of its time on git
and jj processes and on Python's startup. A Rust core behind the Python API
removes neither: the CLI would still start Python to load the backends, and
it would still make every jj call. Python is already memory-safe.

The exception is fingerprinting a large local directory, which is bound by
per-file interpreter work in Python. That loop is now native (`native/`,
phase 1 below): on a million files a warm `status --snapshot` takes 2.1 s
instead of 21, in 0.74 GB instead of 2.7, with the same states, listings and
cache file.

So: jj calls are cut (phase 0), the directory scanner is native (phase 1),
and a Rust core waits until 0.1.0 has frozen the on-disk formats.

## Where a command's time goes

Every invocation pays about 75 ms to start Python and import `tether.cli`
(a bare interpreter: 10 ms). On top of that, in-process, for a dataset of
one memory object and one 200-file directory:

| Command | git: total | git: waiting on git | git processes | jj: total | jj: waiting on jj | jj processes |
| --- | --: | --: | --: | --: | --: | --: |
| `status` | 35 ms | 21 ms | 6 | 87 ms | 72 ms | 8 |
| `status --snapshot` | 40 ms | 22 ms | 5 | 80 ms | 60 ms | 7 |
| `commit` | 106 ms | 77 ms | 16 | 264 ms | 237 ms | 15 |
| `new -b` | 60 ms | 48 ms | 11 | 154 ms | 141 ms | 12 |
| `verify` | 25 ms | 8 ms | 2 | 35 ms | 24 ms | 3 |
| `ops` | 20 ms | 13 ms | 3 | 43 ms | 36 ms | 4 |

tether's own Python is 7–30 ms of each. A git call costs 4–6 ms and a jj
call 7–15 ms: jj loads the repository every time, and most calls snapshot
the working copy. A remote backend adds its round trips on top, and the
first use of a backend in a process imports its library (icechunk: about
100 ms).

In a git repository Python is most of the wall time (of a `status`'s
110 ms, about 90 ms is startup and logic), so only a CLI that does not start
Python at all would make it much faster. In a jj repository the jj calls
dominate, and a Rust tether would make the same calls.

### Fewer jj calls (done)

Tracing `status` in a jj repository showed eleven `jj` calls, about half of
them repeats. This change removes four of them, in three places:

- `jj config get snapshot.auto-track` after `jj config list --user`, on
  every command. One `jj config list --include-defaults` now answers both:
  its rows from the user layer are what `--user` lists (neither shows a
  value a later layer overrides), and its `snapshot.auto-track` row is the
  value that layer resolution picks, as `config get` reported it.
- `current_bookmarks`: `jj log -r @` for the bookmarks, again for
  `position()`'s emptiness, then `jj log -r @-`. One template now returns
  the bookmarks at `@`, `self.empty()` and the parents' bookmarks.
- `alive_commits`: every visible change id, then each asked commit's. One
  `jj log -r '(::) | (c1 | c2 ...)'` with `self.hidden()` now gives both.

| jj command | before | after | processes before | processes after |
| --- | --: | --: | --: | --: |
| `status` | 133 ms | 87 ms | 12 | 8 |
| `ops` | 62 ms | 43 ms | 6 | 4 |
| `init` | 130 ms | 113 ms | 7 | 5 |
| `verify` | 41 ms | 35 ms | 4 | 3 |
| `diff` | 56 ms | 50 ms | 6 | 5 |
| `commit` | 273 ms | 264 ms | 16 | 15 |
| `new -b` | 160 ms | 154 ms | 13 | 12 |

Both columns are in-process medians of seven runs, taken back to back; the
process counts include the one `git cat-file --batch` some commands start.

`tests/test_vcs_calls.py` pins the process count of each command under git
and jj, so a call that creeps back fails a test.

Two cuts were considered and left out. Caching `jj --version` or
`git --version` across processes, keyed by the executable's path and stat,
would save 6–8 ms a command, but on macOS `/usr/bin/git` is an `xcrun` shim
whose git changes with `xcode-select`, and git's minimum version guards
`safe.bareRepository`; a stale "new enough" is not worth 6 ms. The tracked
check on `secrets.toml`, `workspace.toml` and `ops.jsonl` (one `jj file
list` a command) is the trust boundary of the security model and stays.

## Large local trees

The `file` backend's local fingerprint walks the directory, stats every
file, looks each up in a JSON hash cache (`.tether/cache/file-hashes.json`),
sha256-hashes the misses and digests the sorted `(path, hash)` pairs. The
native scanner (`tether_native`, built from `native/`) does the same, by the
same rules, against the same cache file. On 100,000 files of about 80 bytes,
each run in a fresh process, peak memory included:

| | Python | native |
| --- | --: | --: |
| warm fingerprint (every file a cache hit) | 1.78 s, 337 MB | 0.15 s, 107 MB |
| cold fingerprint (every file read) | 5.19 s, 491 MB | 1.63 s, 113 MB |
| diff of two 100k-path listings | 186 ms | 36 ms |

And a whole `tether status --snapshot` of one directory of a million empty
files, the process included:

| | Python | native |
| --- | --: | --: |
| warm | 21.0 s, 2.7 GB | 2.1 s, 0.74 GB |
| cold | 48.7 s, 4.3 GB | 10.8 s, 0.79 GB |

Three findings shaped it. On macOS a per-file `lstat` stops scaling past
the kernel's vnode cache (`kern.maxvnodes`, about 263k here): a million of
them took 4.9 s, where `getattrlistbulk(2)` returns the same attributes a
directory at a time, in 1.2 s for the million; the scanner uses it where
the filesystem supports it, and readdir with `lstat` elsewhere. APFS
serializes concurrent opens, so hashing small files on more than four
threads slowed a cold scan down (1.0 s on 4, 2.6 s on 8, 5 s on 16): the
native scanner hashes on four there and eight elsewhere. And a process-wide
thread pool hangs a process forked after a scan, so its threads live for
one scan, as the Python scanner's do.

`tests/test_native.py` runs both scanners on the same trees -- non-ASCII,
`#` and control characters in names, every kind of symlink, a FIFO, a
symlink loop, unreadable files and directories, the owner marker at the
root and below it -- and requires the same states, listings, cache entries,
errors and listing diffs; `tests/test_golden.py` passes either way. Large
files gain little: `hashlib`'s sha256 already runs at 3.2 GiB/s per core,
and the GIL is released while it does.

## What Rust would and would not buy

Python is a memory-safe language; Rust's guarantee is measured against C
and C++. The memory-unsafe code in a tether process today is inside the
libraries it loads (DuckDB, libpq, OpenSSL), and a Rust tether would load or
link many of the same. What Rust adds is checking:

- data races caught at compile time, for the thread-pool fan-out and the
  caches it shares, which the GIL hides today and free-threaded CPython
  would expose;
- sum types and exhaustive `match` for the `str | Pin | State | None`
  targets and the plan actions, told apart at run time today (93
  `isinstance` checks in `src/`);
- errors as values, and distinct types for pin ids, ref names, dataset ids
  and bookmark slugs, all `str` today;
- compact memory, the 3–4× above.

It would not touch the open correctness rows of `ROADMAP.md` section 1:
Lance's conditional-fork window and Icechunk's non-atomic moves on local
storage (the systems lack a compare-and-swap), `gc` and Neon branches (a
head-check bug), jj repo-level aliases, and handles that outlive a `new`.
A rewrite would also reopen every edge case fixed across sixteen releases.

## Constraints on any Rust design

1. **Handles stay Python objects.** `Repo.open()` returns an icechunk
   `Session`, a `LanceDataset`, a `DeltaTable`, a pyiceberg `Table`, a DuckDB
   connection: objects of the user's own, separately compiled libraries. A
   Rust core cannot construct them; it can return an address (URI plus
   branch, tag or version) for Python to open.
2. **Backends are a Python plugin API.** `ObjectBackend` and
   `tether.testing.run_conformance` are documented for third parties. Keep a
   Python path for backends, with Rust calling them through PyO3, or drop
   third-party backends.
3. **The user's storage engines.** tether drives the icechunk, lance and
   deltalake the user installed, the same versions that write their data.
   Linking the Rust crates instead bundles tether's own copies: format skew,
   lockstep upgrades, and size (165, 108 and 44 MB installed, against 4 MB
   for tether).
4. **jj stays a subprocess.** jj-lib is versioned with the CLI and makes no
   API promise, and its on-disk formats move with it; embedding it pins
   tether to one jj version against a repository the user's jj also writes.
   gix could replace git reads, but the batched `cat-file` reader already
   keeps history walks fast.
5. **Byte-identical formats.** Pin ids, listing names, the manifest hash and
   plan digests hash Python's JSON. Floats are the trap (Python writes
   `1e+16`, serde_json `1e16`), and canonical JSON keeps non-ASCII raw where
   listings escape it. `tests/test_golden.py` pins each format; a port must
   pass it unchanged.
6. **Locks stay `flock`.** Rust's `std::fs::File::lock` (1.89+) is `flock`
   on Linux and macOS, so an old and a new tether still exclude each other,
   and it has a Windows implementation, which would settle the ROADMAP's
   Windows row (Python's `msvcrt` could too).
7. **The tests are the oracle.** Over 550 test functions drive the Python
   API and CLI, and many of their 120 monkeypatches reach engine internals.
   They can check a port only while the Python API stays, and those
   monkeypatches have to become explicit seams first.

## Options

| | Tune the Python | Rust scanner | Rust core, Python shell | Rust throughout |
| --- | --- | --- | --- | --- |
| Moves to Rust | nothing | the `file` backend's walk, hash cache, digests and listing diff | engine, manifests, plans, op log, locks, VCS adapters (about 14k lines) | all of it (about 29k lines), backends on native crates |
| A local command | jj `status` from 133 to 87 ms (done) | unchanged | up to 30 ms less; startup stays | 85–100 ms less; git/jj calls stay |
| 10⁶-file tree | perhaps 2× (a guess) | 10× warm, 4.5× cold, 3.6× less memory (done) | as the scanner | as the scanner |
| Handles, plugins, storage engines | unchanged | unchanged | unchanged | rebuilt; plugins lost; engines bundled |
| Packaging | one pure-Python wheel | a separate platform wheel; tether-vcs stays pure Python | platform wheels; sdist needs Rust | binary plus wheels |
| Effort, roughly | done | done | 2–4 months | 4–6 months or more |
| Risk to 0.1.0 | none | low | high | very high |

## Plan

**Phase 0, in Python (done).** Fewer jj calls; process counts pinned
(`tests/test_vcs_calls.py`); every hashed or committed format pinned
(`tests/test_golden.py`); `benchmarks/run.py`. Still to do: turn the tests'
monkeypatches of engine internals into explicit seams, and stop the `file`
backend keeping an `os.stat_result` and a `Path` per file.

**Phase 1, a Rust scanner (done).**

- `native/` builds `tether-vcs-native` (PyO3 and maturin, abi3 for Python
  3.11+, Linux and macOS). Its `tether_native` module walks, keeps the hash
  cache, hashes, digests, writes the listing text and diffs two listings.
  Listing rows stay in Rust until a commit asks for their text.
- The `file` backend uses it when it is installed and speaks its
  `API_VERSION`; `TETHER_NATIVE=0` turns it off. The Python scanner stays as
  the fallback (no wheel, Windows, a path that is not UTF-8) and as the
  oracle of `tests/test_native.py`.
- The exit criterion held: a warm `status --snapshot` of 10⁶ files takes
  2.1 s and 0.74 GB.
- CI's `native` job runs cargo's checks, on the Rust that
  `native/rust-toolchain.toml` pins, and the whole suite through it on
  Linux and macOS. On a `native-v*` release, `publish-native.yml` builds
  manylinux and musllinux (x86_64, aarch64) and macOS (arm64, x86_64)
  wheels and an sdist, tests every wheel on its own platform against the
  Python scanner, and publishes through a `pypi-native` trusted publisher.
- Still to do: the first release (the PyPI project and its trusted
  publisher), then a `tether-vcs[native]` extra, which can name the package
  only once PyPI has it; until then `uv sync --group native` builds it from
  `native/`.

**Phase 2, after 0.1.0, if the numbers still point at Python.** 0.1.0
deletes the upgrade path (about 1.3k lines and the marked legacy sites) and
freezes `CONFIG_VERSION`; porting earlier ports code that is about to go.
Then the pure-logic modules (manifests and hashing, plan digests, the op
log, locks, workspace state, the VCS adapters) can move to a `tether-core`
crate behind the same Python imports, with the test suite as the oracle.
The engine would follow only behind a Rust `Backend` trait implemented over
the Python backends, with Ctrl-C handled so an interrupted command still
leaves its journal entry `INCOMPLETE`.

**Not planned: Rust throughout.** It pays only if tether becomes a CLI for
people who do not use Python, where a single binary and a few milliseconds
of startup outweigh bundled storage engines, the lost plugin API and a
ported test suite. Today's users drive Icechunk, Lance and Delta from
Python, and `repo.open()` is the product.

## Re-checking

`uv run python benchmarks/run.py` prints startup, each command's time and
git/jj process count, the tree fingerprint by both scanners (`--files`) and
`plan_gc` over a history (`--commits`). When a change moves these, update
the tables here and, for process counts, `tests/test_vcs_calls.py`.
