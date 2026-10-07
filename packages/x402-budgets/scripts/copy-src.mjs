// The source of truth is examples/agent_economy/typescript; copy it in before building.
import { cpSync, mkdirSync, rmSync } from "node:fs";
const from = new URL("../../../examples/agent_economy/typescript/", import.meta.url);
const to = new URL("../src/", import.meta.url);
rmSync(to, { recursive: true, force: true });
mkdirSync(to, { recursive: true });
for (const f of ["hierarchical-budget-provider.ts", "x402-job-budget.ts", "eliza-x402-budgets.ts"]) {
  cpSync(new URL(f, from), new URL(f, to));
}
cpSync(new URL("../index.ts", import.meta.url), new URL("index.ts", to));
