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
uv run pytest
uv sync --all-extras --group docs && uv run great-docs build   # the documentation
```

Tests skip what the machine cannot run rather than fail: the jj-parametrized
tests need `jj` on `PATH` (CI installs the release binary), and the `publish` /
`import` tests need PostgreSQL's `pg_ctl` (see the README's "Development").

## Changelog

Every user-visible change gets an entry under `[Unreleased]` in
[CHANGELOG.md](CHANGELOG.md) ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)),
saying what changed and what a user has to do about it. Mark breaking changes
**breaking**; before 1.0 they can land in any release.
