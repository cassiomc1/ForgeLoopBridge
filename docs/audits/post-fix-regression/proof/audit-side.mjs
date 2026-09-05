// §10.1 sibling A — ForgeLoopAudit, read-only.
//
// Audit is exercised through its OWN vendored ForgeLoop runtime, never through
// the pinned source tree, so this proves the vendored artifact interprets the
// same boundary as the revision that produced the project. ForgeLoopBridge is
// not involved anywhere in this file (§10.1).
//
// Usage: node audit-side.mjs <audit-repo> <project-path> <task-id> <canonical-dir>

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PINNED = JSON.parse(readFileSync(join(HERE, "pinned-revision.json"), "utf8"));

const [audit, target, taskId, canonicalDir] = process.argv.slice(2);

if (!audit || !target || !taskId || !canonicalDir) {
  console.error("audit-side: usage: node audit-side.mjs <audit-repo> <project-path> <task-id> <canonical-dir>");
  process.exit(2);
}

const VENDORED_ROOT = join(audit, "node_modules", "@cassiomc1", "forgeloop");
const VENDORED_CLI = join(VENDORED_ROOT, "src", "cli.js");

// Fail closed rather than degrade into a weaker check.
if (!existsSync(VENDORED_CLI)) {
  console.error(
    `audit-side: the vendored ForgeLoop runtime is missing at ${VENDORED_CLI}.\n` +
      "audit-side: install Audit's dependencies first (npm ci in the Audit repo). " +
      "This suite deliberately refuses to fall back to the pinned source tree, " +
      "because its whole purpose is to test the vendored artifact.",
  );
  process.exit(2);
}

const results = [];
const check = (label, condition, detail = "") =>
  results.push({ label, ok: Boolean(condition), detail });

function treeDigest(dir) {
  const hash = createHash("sha256");
  const walk = (current) => {
    for (const entry of readdirSync(current).sort()) {
      const full = join(current, entry);
      const info = statSync(full);
      if (info.isDirectory()) {
        walk(full);
      } else {
        hash.update(full.slice(dir.length));
        hash.update(readFileSync(full));
      }
    }
  };
  walk(dir);
  return hash.digest("hex");
}

const vendoredCli = (args) =>
  execFileSync("node", [VENDORED_CLI, ...args, "--path", target], {
    encoding: "utf8",
    cwd: target,
  });

const before = treeDigest(join(target, ".forgeloop"));

// A1 — the vendored runtime is the recorded lineage.
const provenance = JSON.parse(readFileSync(join(audit, "schemas", "provenance.json"), "utf8"));
const vendoredPkg = JSON.parse(readFileSync(join(VENDORED_ROOT, "package.json"), "utf8"));
check(
  `A1 vendored runtime matches the recorded lineage ${PINNED.forgeLoopPackageVersion} @ ${PINNED.forgeLoopGitCommit.slice(0, 7)}`,
  provenance.forgeLoopPackageVersion === PINNED.forgeLoopPackageVersion &&
    provenance.forgeLoopGitCommit === PINNED.forgeLoopGitCommit &&
    vendoredPkg.version === PINNED.forgeLoopPackageVersion,
  `provenance=${provenance.forgeLoopPackageVersion} installed=${vendoredPkg.version}`,
);

// A2 — identical protocol-version interpretation (§10.2).
const sourcePi = JSON.parse(readFileSync(join(canonicalDir, "protocol-info.json"), "utf8"));
const auditPi = JSON.parse(vendoredCli(["protocol-info", "--json"]));
check(
  "A2 protocol boundary identical between source revision and vendored runtime",
  JSON.stringify(auditPi) === JSON.stringify(sourcePi),
  `protocolVersion=${auditPi.protocolVersion} packageVersion=${auditPi.packageVersion}`,
);

// A3 — capability interpretation is identical (§10.2).
const featureKeys = (pi) => Object.keys(pi.features).sort().join(",");
check(
  `A3 capability set identical (${PINNED.expectedFeatureCount} keys) and integrationApi v${PINNED.expectedIntegrationApiVersion} on both sides`,
  featureKeys(auditPi) === featureKeys(sourcePi) &&
    Object.keys(auditPi.features).length === PINNED.expectedFeatureCount &&
    auditPi.features.integrationApi.version === PINNED.expectedIntegrationApiVersion,
  `${Object.keys(auditPi.features).length} keys`,
);

// A4 — task identity survives Audit's read-only projection (§10.2).
const taskList = JSON.parse(vendoredCli(["task-list", "--json"]));
const tasks = taskList.tasks ?? taskList;
const found = (Array.isArray(tasks) ? tasks : []).find((entry) => entry.taskId === taskId);
check(
  "A4 task identity preserved through Audit read-only projection",
  Boolean(found),
  `taskId=${found?.taskId} claimState=${found?.claimState} ownershipValid=${found?.ownershipValid}`,
);

// A5 — lifecycle and ownership read the same canonical state (§10.2).
const taskShow = JSON.parse(vendoredCli(["task-show", "--task", taskId, "--json"]));
check(
  "A5 lifecycle phase and canonical ownership read without inference",
  taskShow.taskId === taskId && taskShow.ownershipValid !== false,
  `phase=${taskShow.phase ?? found?.phase} recovery=${JSON.stringify(taskShow.recovery ?? null)}`,
);

// A6 — every command Audit may spawn is read-only.
const allowed = readFileSync(join(audit, "src", "main", "core", "cli", "allowed-commands.ts"), "utf8");
const mutating = [
  "init",
  "advance",
  "complete",
  "clear-state",
  "task-create",
  "task-unlock",
  "route",
  "preflight",
  "activate",
  "run-action",
  "approval-resolve",
];
check(
  "A6 spawn allowlist contains no mutating command",
  mutating.every((command) => !new RegExp(`'${command}'`).test(allowed)),
  `${(allowed.match(/'[a-z-]+'/g) ?? []).length} allowlisted commands`,
);

// A7 — read-only in fact, not only by policy.
const after = treeDigest(join(target, ".forgeloop"));
check("A7 Audit inspection mutated nothing under .forgeloop", before === after, `${before.slice(0, 16)}…`);

// A8 — Bridge is not required for Audit inspection (§10.1).
check(
  "A8 no Bridge dependency in Audit runtime dependencies",
  !Object.keys(
    JSON.parse(readFileSync(join(audit, "package.json"), "utf8")).dependencies ?? {},
  ).some((name) => /bridge/i.test(name)),
);

let failed = 0;
for (const { label, ok, detail } of results) {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"} | ${label}${detail ? `\n     | ${detail}` : ""}`);
}
console.log(`audit-side: ${results.length - failed}/${results.length} PASS`);
process.exit(failed ? 1 : 0);
