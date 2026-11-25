/**
 * bun cli.ts deps <package.json>            are these dependency versions safe to ship?
 * bun cli.ts research "<question>" <url>... answer from these pages, with citations
 * bun cli.ts link <url>                     should I trust this link?
 * bun cli.ts watch <urls.txt> <state.json>  what changed in these docs/changelogs since last run?
 *
 * Needs EVM_PRIVATE_KEY (your wallet with a little USDC on Base), MAX_USD (default 0.01 for the
 * run), and a model: MODEL (default "openai/gpt-5-mini", through the Vercel AI Gateway with
 * AI_GATEWAY_API_KEY, or any AI SDK provider you wire in).
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { reviewDependencies, researchBrief, vetLink } from "./tasks";
import { checkForChanges, summarizeChanges } from "./watch";
import { paidFetchFromEnv } from "./wallet";

const [task, ...args] = process.argv.slice(2);
const { fetch, spent, maxUsd } = paidFetchFromEnv();
const run = { model: process.env.MODEL ?? "openai/gpt-5-mini", fetch };

let result;
if (task === "deps" && args[0]) {
  const pkg = JSON.parse(readFileSync(args[0], "utf8"));
  const deps: Record<string, string> = {};
  for (const [name, range] of Object.entries({ ...pkg.dependencies } as Record<string, string>)) {
    const exact = range.replace(/^[\^~=v]/, "");
    if (/^\d+\.\d+\.\d+/.test(exact)) deps[name] = exact; // exact versions only; ranges skipped
  }
  result = await reviewDependencies(run, Object.fromEntries(Object.entries(deps).slice(0, 20)));
} else if (task === "research" && args.length >= 2) {
  result = await researchBrief(run, args[0], args.slice(1, 11));
} else if (task === "link" && args[0]) {
  result = await vetLink(run, args[0]);
} else if (task === "watch" && args[0] && args[1]) {
  const urls = readFileSync(args[0], "utf8").split("\n").map((l) => l.trim()).filter((l) => /^https?:\/\//.test(l));
  const pages = new Set(urls).size;
  if (Number(maxUsd) + 1e-9 < pages * 0.002) {
    console.error(`watching ${pages} pages costs up to $${(pages * 0.002).toFixed(3)} per check; set MAX_USD to at least that`);
    process.exit(2);
  }
  const state = existsSync(args[1]) ? JSON.parse(readFileSync(args[1], "utf8")) : {};
  const check = await checkForChanges(fetch, urls, state);
  writeFileSync(args[1], JSON.stringify(check.state, null, 2));
  for (const f of check.failed) console.error(`not read (not charged): ${f.url} ${f.error}`);
  result = { text: await summarizeChanges(run.model, check.changed) };
} else {
  console.error('usage: bun cli.ts deps <package.json> | research "<question>" <url>... | link <url> | watch <urls.txt> <state.json>');
  process.exit(2);
}
console.log(result.text);
console.error(`\nspent ${Number(spent()) / 1e6} of ${maxUsd} USD this run`);
