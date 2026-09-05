// §10 cross-repository contract testing — canonical producer side.
//
// Produces, from the EXACT pinned ForgeLoop revision running against a
// disposable project the harness created itself:
//   protocol-info.json  the real public compatibility boundary
//   task-context.json   the real canonical task/context projection
//
// Both are regenerated on every run. Nothing here is a hand-written fixture: if
// the pinned revision cannot be materialized the harness fails closed instead of
// substituting a stored copy.
//
// Usage: node produce-canonical.mjs <forgeloop-root> <project-path> <task-id> <out-dir>

import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

const [forgeloopRoot, projectPath, taskId, outDir] = process.argv.slice(2);

if (!forgeloopRoot || !projectPath || !taskId || !outDir) {
  console.error(
    "produce-canonical: usage: node produce-canonical.mjs " +
      "<forgeloop-root> <project-path> <task-id> <out-dir>",
  );
  process.exit(2);
}

// A trailing separator matters: ForgeLoop resolves package-relative resources
// against packageRoot, so it must name the directory, not a sibling prefix.
const packageRoot = `${resolve(forgeloopRoot)}/`;
const moduleUrl = (relative) => pathToFileURL(join(packageRoot, relative)).href;

const { readForgeLoopIntegrationResource, getForgeLoopPackageVersion } = await import(
  moduleUrl("src/integration.js")
);
const { protocolInfo: buildProtocolInfo } = await import(moduleUrl("src/core/protocol-info.js"));

const protocolInfo = buildProtocolInfo({ packageVersion: getForgeLoopPackageVersion() });
writeFileSync(join(outDir, "protocol-info.json"), `${JSON.stringify(protocolInfo, null, 2)}\n`);

const context = await readForgeLoopIntegrationResource("task/context", {
  projectPath: resolve(projectPath),
  packageRoot,
  taskId,
});
writeFileSync(join(outDir, "task-context.json"), `${JSON.stringify(context, null, 2)}\n`);

console.log(
  `producer: packageVersion=${protocolInfo.packageVersion} ` +
    `protocolVersion=${protocolInfo.protocolVersion} ` +
    `integrationApi=${protocolInfo.features.integrationApi.version} ` +
    `commands=${protocolInfo.commands.length} features=${Object.keys(protocolInfo.features).length}`,
);
console.log(
  `producer: task/context taskId=${context.data.taskId} ` +
    `schemaVersion=${context.data.schemaVersion} ` +
    `protocolVersion=${context.data.protocolVersion} ` +
    `phase=${context.data.phase} nextAction=${context.data.nextAction} ` +
    `resolved=${context.data.executionProfile.resolved}`,
);
