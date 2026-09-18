# Inferrail — Phase 1 audit (pre-change baseline)

Date: 2026-09-18. Read-only pass — nothing in the product or site was
changed to produce this document. Repo state: `main` @ `2dd1a53`,
`pyproject.toml` version `0.4.1`, live PyPI `0.4.1` (confirmed by
installing it fresh — see "Hands-on first-run" below).

## 1. What the homepage currently leads with

The live homepage (`docs/index.html`, deployed to `https://tryinferrail.com/`)
leads with **AP invoice-exception recovery**, not the cost-receipt
promise. Exact current above-the-fold copy:

- `<title>`: "Inferrail — AP invoice-exception recovery"
- H1: "Retry it once, or send it to review. *Decided, executed, recorded.*"
- Lede: "For one eligible invoice-extraction exception, Inferrail
  decides one permitted machine retry vs. your established
  human-review path, executes it, and records the resulting cost and
  outcome."
- The hero's example artifact is an **AP decision record** (work_id,
  failure_type, recommended_action, retry_status, cost) — not a
  gateway cost receipt.
- Primary buttons: "See the full contract" (→ GitHub blob of the AP
  capability doc), "See the examples" (→ GitHub tree), and a ghost
  button "Copy the local demo command" (`pip install inferrail &&
  inferrail ap demo`).

"Know what your AI work costs" **does already exist on the page** —
but as an `<h3>` inside the *second* section (`#gateway`), well below
the fold, described as "Inferrail's original product," i.e. explicitly
framed as secondary/legacy relative to AP. This is the clearest single
signal of the hierarchy inversion this task exists to fix: the exact
phrase the new tagline is built from is already in the code, just
buried.

## 2. Every section on the site, in order (current)

**Homepage (`docs/index.html`):**
1. Masthead/nav — wordmark, links to `#gateway`, `#ap-hosted`, `#agents`, GitHub, and a GitHub blob "Docs" link (no docs index page).
2. Hero — AP framing (above), example AP decision-record card, primary CTAs (both go to GitHub, not to an in-page path).
3. `#gateway` ("Know what your AI work costs") — one card, describing the gateway/receipts as the substrate AP builds on. One link out to the README on GitHub.
4. `#ap-hosted` — three cards: self-serve sandbox (4-command curl walkthrough against a live Render demo), self-hosted deploy instructions, and a Work Economics connector blurb.
5. `#agents` ("Experimental, testnet only") — two cards: Work Economics (x402/testnet), Economic Authority (x402/testnet).
6. Footer — links to AP Exceptions doc, Gateway README, Work Economics, Economic Authority, GitHub, and a link labeled **"Privacy"** that actually points at `SECURITY.md` (mislabeled — the real privacy page, `docs/privacy/usage-ping.md`, isn't linked from the homepage at all).

**Other pages that exist but aren't in the homepage's nav:**
- `/work-economics/` (`docs/work-economics/index.html`) — Work Economics detail page, linked from the homepage's `#ap-hosted` and `#agents` cards, not from the masthead nav.
- `/demo/` (`docs/demo/index.html`) — a 4-step animated walkthrough of Work Economics (not linked from the homepage at all — an orphan page, reachable only by direct URL or the sitemap... which also doesn't list it, see below).
- `/join/` (`docs/join/index.html`) — an email-capture "Get Early Access" form (name/work email/company). **Not linked from the homepage** either — also an orphan page. This is exactly the kind of signup-gate the task says must not sit on the fast path; good news is it currently isn't reachable from the path at all, so there's nothing to remove, just something to not accidentally re-link.
- `docs/comparison.md` — a fair, source-cited LiteLLM comparison (no disparagement found — see "claims" below). Linked from neither the homepage nor its own nav; only reachable via GitHub or direct URL.

`docs/sitemap.xml` lists **only** `https://tryinferrail.com/` — none of `/work-economics/`, `/demo/`, `/join/`, or `/comparison.md` are in it.

## 3. Primary call-to-action

There are effectively **three** competing primary actions in the hero, none of which is a single unambiguous button:
1. "See the full contract" (dark button) → a GitHub markdown blob, not an in-page or in-terminal path.
2. "See the examples" (ghost button) → a GitHub tree listing.
3. "Copy the local demo command" (ghost button, JS-driven) → copies `pip install inferrail && inferrail ap demo` to the clipboard; this is the only one that's actually a fast path to a real command, and it's visually the least prominent of the three (ghost style, third in reading order).

No account/signup is required on any hero path — good, matches the task's requirement already, just needs to be made singular and dominant rather than one of three roughly-equal options.

## 4. Hands-on first-run experience (done fresh, this pass)

Fresh `python3 -m venv` + `pip install inferrail` from live PyPI, no prior state:

- `python3 -m venv venv`: **4.3s**
- `pip install inferrail`: **13.0s**
- `import inferrail; inferrail.__version__` → `0.4.1` (confirms live PyPI matches `main`'s `pyproject.toml`)

**Total: ~17s to a working install** — this part is already fast and frictionless.

From there, zero-key paths:
- `inferrail demo` (offline, canned data): **0.75s**, prints a large, dense report (customer breakdown, work-economics-by-outcome, etc.) — informative but a lot to read for a first 10 seconds, and **not the promise's actual example** (it's about `work_id`/outcomes, not the payload-free receipt itself).
- `inferrail ap demo` (offline, real decision engine, fixture adapter): **0.98s**, produces a real local SQLite store + JSON report.
- `inferrail report` before any real receipt exists: correctly prints a plain "No receipts found... Run some requests through the gateway first" — not an error, not a crash. Good baseline behavior already.

**Where it stops being "under 5 minutes, zero configuration decisions": a real receipt requires a real `OPENAI_API_KEY` or Anthropic key.** This sandbox's `.env.local` contains a literal placeholder (`OPENAI_API_KEY=sk-...`), not a usable key, so **the actual "point an SDK at it, get a real receipt" step could not be completed live in this environment** — flagged here rather than faked; Phase 2/5 will need either a real key supplied by the founder, or a local mock OpenAI-compatible endpoint used honestly as a substitute and labeled as such.

### Concrete friction points found

1. **`inferrail serve --quickstart`'s startup banner is silently lost whenever stdout isn't line-buffered** (piped to a file, `nohup`, backgrounded, Docker, etc.) — confirmed by reproducing with and without `PYTHONUNBUFFERED=1`: without it, none of the six `print()` lines (the quickstart banner) appear at all before the process is killed; with it, they appear immediately. The `print()` calls in `cli/main.py::_cmd_serve` don't set `flush=True`. This directly undermines Phase 2's requirement that the copy-pasteable connection line be reliably visible on startup.
2. **The quickstart banner never actually prints the copy-pasteable `base_url` line** the task requires — today it prints `provider: OpenAI` / `models: passthrough...` / `receipts: <path>`, but never something like `base_url="http://127.0.0.1:8000/v1"` for the OpenAI SDK, and never anything for Anthropic (`ANTHROPIC_BASE_URL=...`) at all.
3. **Quickstart is OpenAI-only.** `config/quickstart.py`'s `build_quickstart_config()` hardcodes a single `openai` provider/route. There is no Anthropic route in the quickstart path today, despite the gateway fully supporting `/v1/messages` passthrough (ADR-0014). Phase 2 needs both.
4. **`inferrail demo` pollutes the real default outcomes file.** Running `inferrail demo` in a directory writes real files at the *default* paths `inferrail work` reads from — specifically `./inferrail-work-outcomes.jsonl` (four synthetic rows: `work-contract-1`, `work-support-1`, `work-unknown-1`, `work-outcome-only-1`). Reproduced fresh: after only running `inferrail demo` (no other command), `inferrail work --all` shows those four synthetic work items as if they were real data, with no "demo" marker distinguishing them. A user who runs `inferrail demo` to try the tool, then later does real work, will see phantom rows mixed into `inferrail work --all` forever (or a collision if they ever legitimately use `work_id=work-contract-1`, etc.).
5. **Budgets cannot be part of the `--quickstart` path at all today.** `BudgetsConfig.enabled=True` requires `receipts.sink: sqlite` (enforced by config validation), but `build_quickstart_config()` hardcodes `receipts.sink: "jsonl"`. The one mode that *does* force `sqlite` and enable budgets, `--app-mode`, is explicitly documented and enforced as **mutually exclusive** with `--quickstart` (`_cmd_serve` errors if both are passed). So "one flag or one line to set a daily ceiling" in the quickstart path is not just missing UX — it's structurally blocked by the current config model and needs a real code change, not just a CLI flag.
6. **`inferrail verify-payload-free` does not exist.** Confirmed (`argument command: invalid choice`). The closest existing things are `docs/PRODUCT.md`'s manual `grep`-based walkthrough and `inferrail telemetry preview` (which proves the *usage-ping* payload, not the *receipt* schema). Nothing today lets a user paste one command's output into a security review to prove the receipt schema itself has no payload field — this is the whole basis of Phase 2's centerpiece command and needs to be built from scratch (though the underlying guarantee is real and already tested: `InferenceReceipt` in `src/inferrail/receipts/schema.py` is a small, explicit Pydantic model with no field capable of holding message content, and `test_inference_receipt_has_no_payload_fields` already exists as a regression test to build on).
7. **`budget set` requires four mandatory flags plus an explicit `--db` path** with no relationship to quickstart's own receipts/data location — there's no single low-friction "just cap today's spend" entry point.

None of these are catastrophic — the underlying engine, receipt schema, and CLI plumbing are all solid and already well-tested (953 tests collected) — but together they mean the *actual* first-time path today is closer to "install (17s) → hit a wall needing a real key or read multiple docs to work out quickstart's OpenAI-only, budget-less, Anthropic-less limits" than "five minutes, zero configuration decisions."

## 5. Claims on the site not directly verifiable in the codebase

Went through `docs/index.html`, `docs/work-economics/index.html`, `docs/comparison.md`, and `README.md` looking specifically for competitor disparagement, privacy/security-barrier overclaims, fabricated metrics, testimonials, or "trusted by" content:

- **No testimonials, customer logos, or user-count claims found anywhere** on the homepage, `/work-economics/`, `/demo/`, or in `README.md`. `docs/PRODUCT.md` explicitly states "Zero customer adoption or savings claims are made about this capability." Good — nothing to remove here.
- **No competitor disparagement found.** `docs/comparison.md` is deliberately fair and source-cited (explicitly says "not a claim about which tool is better overall," cites LiteLLM's own docs with a checked date). This can be reused as-is under "What Inferrail never keeps," per the task's instruction to reuse existing content.
- **No "privacy/security barrier against the provider" overclaim found on the current homepage or `docs/PRODUCT.md`.** Both are already careful: the receipt's own note says "Invoice content and provider credentials never reach an Inferrail-operated service — only identifiers..." (true, scoped to Inferrail's own service, not the provider) and `docs/PRODUCT.md` explicitly says "your provider still receives the real prompt either way — Inferrail is a pass-through gateway to it, not a privacy boundary against it." This is exactly the honest framing the task wants preserved — it already exists, just needs to stay true after the rewrite and be surfaced higher up (task's "What Inferrail never keeps" section).
- **One mislabeled link, not a false claim but worth fixing**: the footer's "Privacy" link points at `SECURITY.md` (vulnerability-disclosure policy), not at the real `docs/privacy/usage-ping.md`. Nothing false is stated, but the label doesn't point where a visitor would expect.
- **Everything else checked against `docs/PRODUCT.md`** (the authoritative scope doc) matched what's actually implemented: OpenAI + Anthropic passthrough, payload-free receipts, `work_id`/outcomes, budgets, the AP module, the sandbox, Work Economics/Economic Authority (both correctly and consistently labeled testnet-only, non-custodial, not real-world spend enforcement).

**Conclusion: the current site is already honest.** The task's instruction to "remove or rewrite every sentence that disparages competitors or claims Inferrail is a security/privacy barrier" turns out to require no deletions — the existing copy already avoids both traps. The work here is reorganization and rewording around the new hierarchy, not damage control.

## 6. Install/usage counting — already exists, partially

This is the biggest thing this audit needs to flag before Phase 4 starts, because **most of Phase 4 already exists**, built to a *different* spec than the one in this task:

- `src/inferrail/usage_ping/` (payload.py, client.py, install_id.py, state.py, receipt_hook.py) — a real, tested, **opt-in, off-by-default** usage ping. `hosted/usage_ping/service.py` — a real FastAPI collector, built but **not deployed anywhere** (confirmed: no `usage_ping.endpoint` shipped by default, per ADR-0019 and `docs/PRODUCT.md`).
- Current payload shape: `install_id` (random uuid4, not hardware-derived — matches this task's requirement), `event` (one of `first_run`/`tool_connected`/`first_receipt`/`budget_created` — **different event names and one fewer event than this task's `install`/`serve_start`/`first_receipt`/`heartbeat`**, and **no heartbeat at all today**), `os`, `inferrail_version`, `ts`. No `python_version` field today (this task requires one).
- **Default posture conflict, the one real decision point**: the existing feature is explicitly, deliberately **opt-in / off by default** (`UsagePingConfig.enabled` defaults to `False`), a design the existing ADR-0019 says was "the founder's explicit," "non-negotiable" requirement before HN launch, directly citing `MISSION.md`'s own stated rule "no telemetry without explicit opt-in (default off)." **This task asks for the opposite posture: on-by-default with opt-out** (`INFERRAIL_TELEMETRY=0`/`--no-telemetry`/`DO_NOT_TRACK=1`). These two documents actively disagree with each other, and I'm not resolving that silently — flagging it here for an explicit decision before Phase 4 writes any code, per this task's own instruction not to reopen a documented decision without saying so plainly. (Everything else about the *mechanics* — random non-hardware id, no prompts/keys/receipts/IPs, fails silently, never blocks startup, a typed/tested payload shape, no IP persistence at the collector — is already fully aligned between the existing ADR-0019 design and this task's spec, and can be extended rather than rebuilt.)
- Also currently scoped to `--app-mode` only (`first_run` fires from app-mode startup; `tool_connected`/`first_receipt` fire from a receipt-sink wrapper only wired in under app-mode). This task wants it on plain `inferrail serve` too, which is a real extension, not just a rename.
- Deployment status: the collector code exists and is tested but **has never been deployed** — `PROGRESS.md`'s "HUMAN ACTION NEEDED" section says this explicitly and asks the founder to deploy it to Render and report back a URL, which (per that file) had not happened as of the last session recorded there.
- **Zero-telemetry layer (PyPI download stats) does not exist yet** — no `scripts/owner_stats.py`, no `make stats` target. `pypistats.org`'s API is reachable from this environment (confirmed `200` on `https://pypistats.org/api/packages/inferrail/overall`), so this part is straightforward to add.

## 7. GitHub link — checked live, does **not** currently 404

The task states the GitHub link currently returns 404. Checked directly, this pass:

- `https://github.com/domondi1/inferrail` → **200**
- Every specific blob/tree link used on the homepage (`docs/capabilities/ap-invoice-exception-recovery.md`, the AP examples tree, `README.md`, `hosted/ap_exceptions/README.md`, `SECURITY.md`, `docs/capabilities/economic-authority.md`) → **200**
- `pyproject.toml`'s `Repository`/`Issues` URLs both point at the same `domondi1/inferrail` — consistent, no typo found.
- PyPI project page and PyPI JSON API → **200**.
- Live homepage and its other pages (`/`, `/work-economics/`, `/demo/`, `/join/`) → **200**.

I can't explain a prior 404 from this session — either it was a transient GitHub issue, the repo was briefly private/renamed at some point before this session, or the report is describing a different link than the ones on the current homepage/PyPI page. Flagging this rather than guessing further: **worth confirming with the founder which exact URL was seen returning 404 and when**, since nothing reachable from the current site or PyPI page reproduces it right now.

## 8. Baseline numbers for Phase 5 comparison

- Test suite, run to completion (`pip install -e ".[dev,mcp,ap]"` then `pytest -q`): **852 passed, 11 skipped** (skips are the `OPENAI_API_KEY`-gated live-provider tests), 0 failures.
- `ruff check .`: all checks passed. `mypy src/inferrail`: 0 errors, 87 source files.
- Fresh install time: **~17s** (venv + `pip install inferrail`), independently reproduced twice (once from local source, once from live PyPI) — consistent.
- Time to *first real receipt*: **not completed this pass** — blocked on a real provider key not being available in this sandbox (`.env.local` holds a literal placeholder, `OPENAI_API_KEY=sk-...`). What *was* verified live: `inferrail serve --quickstart` starts, accepts a request, forwards it to OpenAI, and produces a real (failed, `status: error`) receipt end-to-end when given a syntactically-valid-but-wrong key — confirming the request→receipt pipeline works; only the "successful, priced" case needs a real key. `inferrail report --by <dim>` and `inferrail work <id>` were both confirmed to produce meaningful, non-empty output from that one real (failed) request, when the right attribution headers are sent (`X-Inferrail-Attribute-Customer`, `X-Inferrail-Attribute-Work-Id`) — today's `--quickstart` banner doesn't tell a new user these headers exist, which is its own friction point worth fixing in Phase 2.
- `pyproject.toml` version: `0.4.1`. Live PyPI: `0.4.1` (verified, not assumed).

## 9. What this means for scope going in

Nothing here changes the plan in the task — it confirms the reorganization is real work (current hierarchy genuinely leads with AP, not cost receipts) but the *honesty* bar is already met (no disparagement, no fabricated claims, no privacy overclaims to walk back). The one decision that needs to be surfaced before writing Phase 4 code, rather than silently overridden, is the opt-in-vs-opt-out default conflict between the existing ADR-0019 and this task's spec (§6 above).
