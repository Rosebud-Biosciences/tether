# Security policy

## Reporting a vulnerability

Report it privately: this repository's **Security** tab, **Report a
vulnerability**. Only the maintainers can read the report. Please do not open
a public issue, pull request or discussion about it.

Fixes land on `main` and in the next release; only the latest release is
supported.

## Scope

In scope: anything in this repository that decides what tether reads, writes
or deletes in your stores and repositories, or what it does with the
credentials it is given (the Neon API key, cloud credentials, the untracked
`.tether/secrets.toml`) -- the library and CLI under `src/tether/` -- and the
workflow that builds and publishes the package to PyPI.

Out of scope: vulnerabilities in the services and formats tether drives (Neon,
S3 and other object stores, Iceberg catalogs, Lance, Icechunk, Delta, Zarr,
git and jj; report those upstream) and in its dependencies.

tether acts with whatever credentials its environment holds: it can delete
what they can delete. Give a CI job or a preview the narrowest role that does
its job.
