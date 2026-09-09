# ForgeLoopBridge 2.2.0 Release Validation

Date: 2026-09-09

Branch: `codex/release-2.2.0`

Release-preparation commit: `148a5c83f90c04da4fa275f57200d03c93847ee4`

Previous main baseline: `59ce5ebab3a6c8433dde4bba37c759fd0d6e4289`

Version-introduction baseline: `a0cd35f853df52f4dcc55ca2d001627de75b138e`

## Verdict

**PASS WITH FOLLOW-UPS**

The 2.2.0 candidate is internally consistent and ready for a release pull request. The remaining follow-ups are hosted exact-head CI and package-artifact smoke validation. No tag, package publication, GitHub Release, or deployment was performed.

## Baseline and version decision

- `2.1.3` is the last version recorded in `pyproject.toml` and `CHANGELOG.md` before this candidate.
- The repository has no formal Git tag or GitHub Release for `2.1.3`; the auditable version-introduction commit is `a0cd35f`.
- Main was current at `59ce5eb` before the release work.
- The change surface since the version-introduction baseline includes bounded Worker modes, the optional Live Execution Observer, the executable ForgeLoop compatibility gate, ForgeLoop 1.11.1 synchronization, documentation/audit corrections, the architecture asset, and the light/dark web theme.
- The change is release-worthy and uses a minor version bump to `2.2.0` because it adds opt-in capabilities and a user-visible interface theme while preserving the existing Bridge API and typed protocol contracts.

## Compatibility and authority boundaries

- Local ForgeLoop CLI: `1.11.1`.
- Protocol version: `1`.
- Integration API version: `1`.
- Consumed context schema and task/context feature versions remain version `1`.
- `repositoryIndex` remains a required, provider-neutral ForgeLoop capability and is outside the consumed-feature compatibility tuple.
- The compatibility gate is fail-closed for unknown declared consumed versions and remains independent of additive repository-index capabilities.
- ForgeLoop lifecycle, completion, evidence, and repository-index authority remains ForgeLoop-owned. Bridge messages remain coordination copies.
- Repository Search remains discovery-oriented and is not completion or evidence authority.
- Bridge does not own tgrep, the persistent repository-index backend, ForgeLoop private search IPC, or a replacement search protocol.

## API, schema, and persistence review

- Existing HTTP endpoints, status semantics, authentication behavior, SSE delivery, retry behavior, and typed message validation remain compatible.
- `BRIDGE_API_VERSION` is `2.2.0` in the status surface; this is informational and does not change the protocol version.
- Typed Message Schema remains v1.
- SQLite schema and migrations are unchanged from the 2.1.3 baseline.
- Migration tests pass and no destructive migration was introduced.
- The observer implementation is isolated from the Bridge API and database authority.

## Worker and observer review

- Worker supports `daemon`, `once`, and bounded idle-poll modes.
- Idle bounds are based on consecutive idle polls, not an unsafe absolute runtime assumption.
- Cursor advancement occurs only after safe handoff; bootstrap and exit/error markers remain stable.
- Typed outbox delivery preserves at-least-once behavior, bounded retries, Retry-After handling, and quarantine for permanent client failures.
- The observer is disabled by default, read-only, E2EE-required, allow-listed, bounded, shell-free, and does not persist credentials or claim lifecycle authority.
- Observer startup, preflight, stderr, signal, cleanup, and no-rerun behavior are covered by the existing tests.

## Web interface and documentation

- `static/index.html` now provides a shadcn-inspired semantic token system with light and dark palettes, system preference detection, persisted preference, and an accessible theme toggle.
- Focus states use visible ring styling and the toggle exposes `aria-pressed`, an accessible label, and a keyboard-operable button.
- README release metadata, API examples, current ForgeLoop synchronization details, and changelog content now describe `2.2.0`.
- Historical `2.1.3`, 1.10.x, and prior validation references remain intentionally preserved as historical records.
- `assets/banner.png` is `1672 x 941`, RGB PNG, SHA-256 `0c5981e68a26d3dd5084d58e9a1f87b51975860a04939f49159bfe9febe514b6`. The originally supplied Downloads copy was unavailable during this final pass, so a fresh byte comparison is recorded as not repeatable from the current filesystem.

## Security review

- Authentication material is rejected from outbox payloads and is not persisted by the observer.
- Provider URLs are HTTPS-only where required; observer URLs are bounded and allow-listed.
- Observer subprocesses use argument arrays with `shell=False` and bounded timeouts.
- SSE and request handling retain the existing authentication, rate-limit, and ticket protections.
- No new Bridge authority, credential store, or unrestricted execution path was introduced.

## Validation evidence

Commands run on the candidate branch:

```text
python3 -m pytest -q
391 passed in 36.09s

ruff check .
All checks passed!

python3 scripts/check_frontend_syntax.py
Frontend inline JavaScript syntax is valid.

git diff --check
passed
```

The focused documentation and frontend invariant suite also passed before the final full-suite run. The configured hosted CI matrix remains the authoritative check for Python 3.12/3.13 across Ubuntu, Windows, and macOS.

## Follow-ups and release controls

- **P0:** none found.
- **P1:** none found.
- **P2:** none found.
- **P3:** package artifact/build smoke is not verified because the repository has no configured build backend or release workflow and the local environment does not provide the `build` module. This is a release-process follow-up, not a runtime regression.
- **Not verified:** hosted CI for the final pull-request head; package artifact build/publication.
- **Verified:** local tests, lint, frontend syntax, documentation invariants, compatibility boundary, authority-boundary review, security review, and working-tree diff hygiene.

The release pull request may be opened after this report is committed. Merge should be considered only after every required check reports success for the exact final PR head. Publication, tagging, GitHub Release creation, and deployment remain explicitly out of scope for this validation.
