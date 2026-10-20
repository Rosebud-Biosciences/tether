# Contributing

Issues and pull requests are welcome. For anything larger than a fix, open an
issue first so the design can be agreed before the code; [ROADMAP.md](ROADMAP.md)
says where the project is going. Security problems go through
[SECURITY.md](SECURITY.md), never a public issue. Everyone taking part follows
the [Code of Conduct](CODE_OF_CONDUCT.md).

## The CI gates

A pull request must pass what CI runs ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)
and [`docs.yml`](.github/workflows/docs.yml)). From the repository root:

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest -n auto
uv sync --all-extras --group docs && uv run great-docs build   # the documentation
```

Tests skip what the machine cannot run rather than fail: the jj-parametrized
tests need `jj` on `PATH` (CI installs the release binary), and the `publish` /
`import` tests need PostgreSQL's `pg_ctl` (see the README's "Development").

A change to the native scanner in [`native/`](native/), or to what it mirrors
in `tether.backends.file`, also has to pass CI's `native` job. That needs
rustup, which installs the Rust that `native/rust-toolchain.toml` pins on
first use:

```bash
(cd native && cargo fmt --check && cargo clippy --all-targets --features python -- -D warnings && cargo test)
uv sync --all-extras --group native   # builds tether-vcs-native into the environment
uv run pytest                         # the file backend now scans through it
```

`tests/test_native.py` runs both scanners on the same trees and requires the
same states, listings, cache file and errors; `TETHER_NATIVE=0` puts the whole
suite back on the Python scanner.

Every wheel ships `native/THIRD-PARTY-NOTICES`: the licenses of the crates
compiled into it. When a crate comes, goes or changes its license text, the
`native` job fails until the file is regenerated with
[cargo-about](https://github.com/EmbarkStudios/cargo-about) 0.9.2
(`cargo install --locked cargo-about@0.9.2`):

```bash
(cd native && cargo fetch --locked && cargo about generate --features python --frozen --fail about.hbs -o THIRD-PARTY-NOTICES)
```

## Changelog

Every user-visible change gets an entry under `[Unreleased]` in
[CHANGELOG.md](CHANGELOG.md) ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)),
saying what changed and what a user has to do about it. Mark breaking changes
**breaking**; before 1.0 they can land in any release.
