# ForgeLoopBridge 2.2.0 Post-Release Validation

Validation date: 2026-09-09

## Release identity

| Item | Verified value |
| --- | --- |
| Release source commit | `ba640352bcb2a41c5b86539be14c0ff5d81fab1d` |
| Pull request | [#44](https://github.com/cassiomc1/ForgeLoopBridge/pull/44), merged |
| Tag | `v2.2.0` |
| Tag target | `ba640352bcb2a41c5b86539be14c0ff5d81fab1d` |
| GitHub Release | [ForgeLoopBridge 2.2.0](https://github.com/cassiomc1/ForgeLoopBridge/releases/tag/v2.2.0) |
| Release state | Published, non-draft, non-prerelease, latest |
| Published at | `2026-09-09T13:18:18Z` |

The annotated tag was fetched from `origin` and dereferenced to the exact
merged-main release commit. The local and remote branch invariant is preserved:
only `main` exists locally and only `origin/main` exists remotely.

## Source consistency

- `pyproject.toml` reports package version `2.2.0`.
- `CHANGELOG.md` has the `2.2.0 - 2026-09-09` release heading.
- `README.md` identifies the current Bridge release as `2.2.0`.
- The release tag, release notes, package metadata, changelog, and README all
  refer to ForgeLoopBridge `2.2.0`.

## Verification results

- Local test suite: `391 passed in 37.07s`.
- Ruff: passed.
- Frontend inline JavaScript syntax check: passed.
- `git diff --check`: passed.
- High-signal credential scan: no credential material found.
- Authority-boundary review: Bridge remains coordination-only; Repository Search
  is discovery context rather than verification evidence; ForgeLoop retains
  lifecycle, evidence, and completion authority; Bridge does not manage tgrep or
  own the Repository Index.

## ForgeLoop compatibility

The installed ForgeLoop CLI reports `1.11.1`. Its canonical
`protocol-info --json` reports Protocol v1, reads Protocol v1, Integration API
v1, and `repositoryIndex` v1 with provider-neutral search managed by ForgeLoop
(`microsoft/tgrep`). The Bridge compatibility boundary classified that live
capability payload as `SUPPORTED` with no reason.

## Package rebuild and contents

The wheel and sdist were rebuilt from the exact release source commit with
`python -m build --sdist --wheel`.

Fresh rebuild artifacts:

| Artifact | Size | SHA-256 |
| --- | ---: | --- |
| `forgeloopbridge-2.2.0-py3-none-any.whl` | 81,503 bytes | `a933863ba2bf7383caf3d2ccaf763b576d13a30562e8dbfc899e3b476ecbe31f` |
| `forgeloopbridge-2.2.0.tar.gz` | 176,941 bytes | `3e9af7b0b1bbf2cf490af6c8b367df63af18d1d9412e56b6b0d57c08a0825b6f` |

Published GitHub Release assets were independently downloaded and hashed:

| Artifact | Size | Published SHA-256 |
| --- | ---: | --- |
| `forgeloopbridge-2.2.0-py3-none-any.whl` | 81,503 bytes | `94d2ede3314d5f68a2326f7db602dd88e2e59f4ae9f966e1250deaa4ee544f28` |
| `forgeloopbridge-2.2.0.tar.gz` | 176,912 bytes | `5c5f796639eb1737596431d41bce92eda9be9fd6d6459d8d7f3405cb45a2236b` |

The fresh and published archives contained the same package members and
content: 15/15 wheel members and 27/27 sdist file members matched. The two
archive-level hash sets are recorded separately because they are separate
archive builds.

Package-content inspection found no Git metadata, test caches, coverage output,
temporary SQLite files, logs, secrets, local paths, validation worktrees, or
Python cache directories.

## Clean-install smoke

The freshly built wheel was installed into a new virtual environment from a
separate working directory with `PYTHONPATH` cleared and source-tree imports
unavailable. The following passed:

- installed distribution version `2.2.0`;
- `bridge_protocol` and `main` imports;
- FastAPI application creation;
- SQLite initialization and write/read round-trip;
- installed Uvicorn startup;
- `/healthz` response;
- `/api/status` reporting Bridge API `2.2.0` and Typed Message Schema `[1]`;
- installed static HTML and vendor assets.

## SQLite migration compatibility

An existing legacy database with the 2.1.3-compatible base schema was opened by
the installed application. Additive columns and indexes were created without
loss of the existing row; a new row was then written and read successfully.

`NO SCHEMA MIGRATION REQUIRED`

## Exact merged-main CI

All observed workflows targeted the exact release commit
`ba640352bcb2a41c5b86539be14c0ff5d81fab1d`:

- [CI run 34355953443](https://github.com/cassiomc1/ForgeLoopBridge/actions/runs/34355953443): passed — Ubuntu Python 3.12, Ubuntu Python 3.13, Windows Python 3.12, and macOS Python 3.12.
- [CodeQL run 34355953095](https://github.com/cassiomc1/ForgeLoopBridge/actions/runs/34355953095): passed — Actions and Python analysis; aggregate workflow conclusion passed.
- [Dependency Graph run 34355958530](https://github.com/cassiomc1/ForgeLoopBridge/actions/runs/34355958530): passed.

No failed or pending mandatory checks were observed for the exact release
commit. CI emitted a non-blocking Node.js 20 action-runtime deprecation
annotation for future maintenance.

## Publication and deployment boundaries

- **Python registry publication:** `NOT CONFIGURED`. Nothing was published to
  PyPI, TestPyPI, or another Python package registry; package files are attached
  only to the GitHub Release.
- **Production deployment:** none performed. No service restart, production
  database change, DNS change, server configuration change, or container
  deployment was performed.

## Required post-release answers

1. `v2.2.0` was created: **yes**.
2. Its exact target is `ba640352bcb2a41c5b86539be14c0ff5d81fab1d`.
3. It matches the validated merged-main release commit: **yes**.
4. GitHub Release `ForgeLoopBridge 2.2.0` was published: **yes**.
5. Draft: **no**.
6. Prerelease: **no**.
7. Latest: **yes**.
8. `pyproject.toml` reports `2.2.0`: **yes**.
9. `CHANGELOG.md` reports `2.2.0`: **yes**.
10. README identifies current Bridge release `2.2.0`: **yes**.
11. Package build from the exact release source succeeded: **yes**.
12. Clean-install smoke passed: **yes**.
13. SQLite remains backward-compatible: **yes**.
14. ForgeLoop 1.11.1 compatibility remains `SUPPORTED`: **yes**.
15. A Python package registry was used: **no**; publication is not configured.
16. Production deployment was performed: **no**.

## Final verdict

**PASS WITH FOLLOW-UPS**

The only release follow-up is the intentionally unconfigured Python registry
publication path. No P0, P1, or P2 blockers remain; P3 is limited to that
publication follow-up.
