# Evidence Manifest — Post-Fix Real-World Regression (2026-09-02)

Raw execution logs are intentionally **not** committed to the repository
(the repo ignores `*.log`). They are preserved in the local post-fix
regression evidence archive. This manifest records their SHA-256 hashes,
purpose, and the audit claims each one supports. Compact structured
artifacts (`engineer-*.json`, `forgeloop-protocol-info.json`) are
committed alongside this manifest.

- Sensitive-data review: PASS (no raw Bearer tokens, API keys, or
  passwords; tokens appearing in logs were redacted to
  `WORKER_TOKEN_REDACTED` / `ENGINEER_TOKEN_REDACTED` before archiving;
  remaining 64-hex strings are ForgeLoop ledger hashes/fingerprints).
- Archive: local post-fix regression evidence archive
  (source: `real-test/evidence/post-fix-regression/`, 2026-09-02 run).

## Raw logs (archived outside Git)

### worker1.log

- Purpose: First fresh bounded Worker turn; proves WAITING_FOR_ENGINEER
  and autonomous exit.
- Included in Git: No
- Size: 480514 B
- SHA-256: `f2a40519c1d0071c22819d877b2359b686c84a1ed4d00c95cc814957d426e69b`
- Sensitive-data review: PASS
- Claims supported:
  - Worker discovered task through Bridge
  - Worker posted exactly one WAITING_FOR_ENGINEER
  - Worker exited without Engineer deadlock

### worker2.log

- Purpose: Second fresh Worker turn; proves fresh-session resume and
  exact canonical-blocker reporting.
- Included in Git: No
- Size: 462969 B
- SHA-256: `b122f57152d4fdd7a47c64d5a612102e0afd98c339cac5201123151edcbbb84d`
- Sensitive-data review: PASS
- Claims supported:
  - Fresh session consumed only the new Engineer review (reply chain)
  - Original TASK_REQUEST was not reprocessed
  - Canonical reason code reported exactly once, no fabricated authority

### worker3.log

- Purpose: Third fresh Worker turn; full TaskVault implementation
  (terminated by harness timeout before final Bridge post).
- Included in Git: No
- Size: 1647697 B
- SHA-256: `5402a626abc600197e5195054d0167175ffe56db64f0325411de5f3731c180cc`
- Sensitive-data review: PASS
- Claims supported:
  - Linear productive progress (contract → route → preflight → code →
    lint → tests → advance), no polling loop
  - Harness timeout terminated a productive turn (orchestration finding)

### worker4.log

- Purpose: Fourth fresh Worker turn; verification and final-review request.
- Included in Git: No
- Size: 624795 B
- SHA-256: `77eb6318962de9e9dc989ca5c960b7738a4619f63dc77f017f39f6a7e5d6c4f9`
- Sensitive-data review: PASS
- Claims supported:
  - Fresh-session resume without reprocessing
  - 8/8 acceptance verification before WAITING_FOR_ENGINEER

### worker5.log

- Purpose: Fifth fresh invocation; terminated by provider quota error.
- Included in Git: No
- Size: 383972 B
- SHA-256: `bc6f089f015ca6c2a991dfbe3d6e48b6ae6d977cf9cd101b678c5e6d204eb871`
- Sensitive-data review: PASS
- Claims supported:
  - Provider quota failure is fail-closed (exit 1, nothing posted)

### worker6.log

- Purpose: Sixth fresh Worker turn; provenance fix and canonical completion.
- Included in Git: No
- Size: 780733 B
- SHA-256: `e9650e9675e33fe77b2f836cd59f151cc6450eb5f358a9ec92ea7ee29d185365`
- Sensitive-data review: PASS
- Claims supported:
  - `run-check` provenance resolution of E_EXECUTION_REF_INVALID
  - Canonical completion VALID/COMPLETE/VALID + NONE/terminal
  - Single COMPLETE_REPORTED post, autonomous exit

### bridge-server.log

- Purpose: Experiment Bridge server log (port 8000).
- Included in Git: No
- Size: 2985 B
- SHA-256: `843f7abec4cbccdcc3d4d7316dca4d7aeae5d32a8868c325f2cfd1207ae70817`
- Sensitive-data review: PASS
- Claims supported:
  - Bridge uptime across all Worker turns and the restart test

### bridge-neg-server.log

- Purpose: Negative-test Bridge server log (port 8002).
- Included in Git: No
- Size: 1921 B
- SHA-256: `0075d2997814958c547b191d4dc4a43e658ae1ae899f5ff0baabcd784bf490b7`
- Sensitive-data review: PASS
- Claims supported:
  - Isolated negative-test environment

### bridge-restart-check.log

- Purpose: Pre/post restart message inventory.
- Included in Git: No
- Size: 467 B
- SHA-256: `ba4c29c9e0e0cb4726e3e9d91507678175df3c6e9fe7f87b9464d440fe466740`
- Sensitive-data review: PASS
- Claims supported:
  - Restart persistence (IDs, keys, correlation stable)

### mode-once-1.log / mode-once-2.log

- Purpose: `once` run-mode verification (consume + no-reconsume).
- Included in Git: No
- Sizes: 435 B / 146 B
- SHA-256: `af36a901d6d91ebc07915976cc6960c0a443f159873c0df1b074d283446f5ac3` /
  `ff0f15d55a3d04dd8a29c46a099525eee76ca572eeaa6c474f0947e9d9eeac1e`
- Sensitive-data review: PASS
- Claims supported:
  - ONE_SHOT_COMPLETE, second run handled=0

### mode-bounded-1.log

- Purpose: `bounded` run-mode verification (idle reset + bound).
- Included in Git: No
- Size: 461 B
- SHA-256: `562e5baaa56a38639c1474fe4d3497fed6e3de6211c5528c355243b72e7075da`
- Sensitive-data review: PASS
- Claims supported:
  - New input resets idle window; IDLE_BOUND_REACHED after 2 idle polls

### mode-daemon.log

- Purpose: `daemon` run-mode verification (stays alive, intentional kill).
- Included in Git: No
- Size: 74 B
- SHA-256: `61438e9e66766004590335ec4c14edd6d7064e4727a6976c8ba34f53481ae1b2`
- Sensitive-data review: PASS
- Claims supported:
  - Continuous transport behavior preserved

### neg-gh-1.log / neg-gh-2.log

- Purpose: INVALID-integrity handoff-failure runs (identical outputs).
- Included in Git: No
- Size: 1204 B each
- SHA-256: `ba6a8ab7a459d7950f2c3158421372396c8a46f130489b9a2a8246fc8ab62d15` (both)
- Sensitive-data review: PASS
- Claims supported:
  - Non-zero exit, cursor held, redelivery eligibility

## Structured evidence (committed in Git)

- `engineer-task-request.json` — Bridge message 1 (TASK_REQUEST, VALID)
- `engineer-review-1.json` — Bridge message 3 (REVIEW_RESULT, VALID)
- `engineer-decision-1.json` — Bridge message 5 (repair directive)
- `forgeloop-protocol-info.json` — ForgeLoop 1.10.0 / protocol v1 baseline
