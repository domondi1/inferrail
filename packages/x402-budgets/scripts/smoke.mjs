// Import the built package the way a user would and exercise the core path.
import { HierarchicalBudgetProvider, elizaX402Budgets, withJobBudgets } from "../dist/index.js";
const p = new HierarchicalBudgetProvider();
p.open("job", 10n);
const c = p.delegate("job", "child", 4n);
if (!c.ok || !p.reserve(c.childRef, 4n).ok || p.reserve(c.childRef, 1n).ok) throw new Error("smoke: budget rules broken");
if (typeof withJobBudgets !== "function" || typeof elizaX402Budgets !== "function") throw new Error("smoke: exports missing");
console.log("smoke ok");
