// Writes the task contract through ForgeLoop's OWN writeContract API so the
// artifact is canonical and schema-validated, never hand-synthesized.
//
// Usage: node write-contract.mjs <forgeloop-root> <project-path> <task-id>

import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

const [forgeloopRoot, projectPath, taskId] = process.argv.slice(2);

if (!forgeloopRoot || !projectPath || !taskId) {
  console.error("write-contract: usage: node write-contract.mjs <forgeloop-root> <project-path> <task-id>");
  process.exit(2);
}

const packageRoot = `${resolve(forgeloopRoot)}/`;
const { writeContract } = await import(
  pathToFileURL(join(packageRoot, "src/core/contract.js")).href
);

// Fixed content, so the routing decision and therefore the resolved execution
// profile are reproducible across runs.
const written = await writeContract(
  resolve(projectPath),
  {
    schemaVersion: 1,
    protocolVersion: 1,
    taskId,
    objective: "Add a bounded regression test for the cross-repository contract suite.",
    deliverables: ["tests/test_xrepo.py"],
    constraints: ["No external services.", "No mutation of canonical state from a companion."],
    risks: ["accessibility"],
    verification: [{ id: "unit", text: "pytest passes" }],
    successCriteria: [{ id: "green", text: "The suite is green on all supported platforms" }],
    stopConditions: ["A canonical gate reports INVALID."],
    unresolvedDecisions: [],
    sourceRefs: ["FORGELOOP_ECOSYSTEM_SYNC_RUNBOOK_REVISED.md#10"],
  },
  packageRoot,
  { taskId },
);

console.log(`contract written: ${written.relativePath ?? written.path ?? "(ok)"}`);
