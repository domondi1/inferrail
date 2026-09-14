# 0013. A WAL-mode SQLite receipts store, opt-in alongside JSONL

## Status

Accepted

## Context

`MISSION.md`'s v0.3.0 calls for "a SQLite receipts store (WAL) as a
first-class sink with JSONL import/export; indices on
`ts`/`work_id`/`project`/`model`; existing reports work over it." The
motivating gap: the only receipts sink today is
`sinks.JSONLReceiptSink`, and every reader (`inferrail
report`/`transaction`/`work`, via `cli.report.load_receipts`) loads the
*entire* file into memory and re-scans it in Python for every query.
That's fine at demo/small-deployment scale and keeps v0.1's dependency
surface minimal, but it doesn't scale to a real install accumulating
receipts over weeks, and it gives later v0.3.0 units (budgets needing a
fast "spent so far" check; the local control API's paginated receipt
query) nothing to build on.

`work_id` and `project` are not schema fields on `InferenceReceipt` —
they're conventional keys inside its open-ended `attributes: dict[str,
str]` bag (see `cli.attributes`, `work.builder._matching_receipts_for_work`).
Indexing them in SQLite therefore requires a decision: index expressions
into a JSON blob (via SQLite's JSON1 extension), or extract them into
their own columns at write time.

## Decision

**New sink, not a replacement.** `receipts.sink` gains a third value,
`sqlite`, alongside the existing `jsonl` and `none`; `jsonl` stays the
default. `inferrail.receipts.sqlite_store.ReceiptsStore` implements the
same `sinks.ReceiptSink` protocol (`emit`) as `JSONLReceiptSink`, so
`sinks.build_receipt_sink` just returns one or the other — the gateway's
own request-handling code has zero awareness of which sink is active.

**Same transactional discipline as this repo's other SQLite stores**
(`inferrail.ap.store.RecoveryStore`, `hosted/a2a_economic_authority`'s
stores): one connection per call, `PRAGMA journal_mode=WAL`, a 30s
`busy_timeout`, `BEGIN IMMEDIATE` around the one write path. `emit` is
idempotent on `receipt_id` (`INSERT OR IGNORE`) — replaying a receipt
(a retried import, an unsure gateway worker) never duplicates a row.

**`work_id`/`project` are extracted into their own indexed columns at
write time**, not indexed via JSON1 expressions: this repo's SQLite
stores don't currently depend on JSON1 being compiled into whatever
Python's `sqlite3` links against, and a real column is simpler to
reason about, test, and query than an expression index. `attributes` as
a whole is still stored in full (as a JSON column) so every other
attribution key round-trips exactly — `work_id`/`project` columns are a
read-optimization *derived from* that JSON at write time, never a second
source of truth read back from instead of it (`_row_to_receipt` always
reconstructs `attributes` from the JSON column alone).

**Existing reports work over either sink via detection, not a new
flag.** `cli.report.load_receipts` (already the single shared read path
for `report`, `transaction`, and `work`) sniffs the target file's own
SQLite magic bytes (`sqlite_store.looks_like_sqlite`) rather than
trusting an extension or requiring a `--backend` flag. This means zero
new CLI surface for the three existing read commands — `--receipts
some/path` just works whichever sink produced it — and the pure
aggregation functions (`aggregate`, `build_work_summary`,
`build_transaction`, etc.) are completely unchanged: they already only
ever consume a `list[InferenceReceipt]`.

**JSONL import/export are their own small CLI surface**
(`inferrail receipts import --jsonl <path> --db <path>` /
`inferrail receipts export --db <path> --jsonl <path>`), not automatic
migration on first use — an operator switching `receipts.sink` from
`jsonl` to `sqlite` in `inferrail.yaml` should not silently lose or
duplicate history; making the migration an explicit, safe-to-rerun
command (import is idempotent; export only ever appends) keeps that
choice visible and reversible.

## Consequences

- An operator who never touches `receipts.sink` sees no behavior change
  at all — `jsonl` stays the default, byte-for-byte the same sink as
  before this change.
- `inferrail report`/`transaction`/`work` gained a scaling path (an
  indexed SQLite file) without any of their own code changing beyond
  `load_receipts`'s one dispatch point.
- Future v0.3.0 units (budgets' "spent so far" check, the local control
  API's receipt query/pagination) have `ReceiptsStore.query()` to build
  on instead of re-deriving their own SQLite access pattern.
- A receipt written by one sink is invisible to a command reading the
  other until an explicit `inferrail receipts import`/`export` — this is
  intentional (see "Decision" above), but worth remembering if a report
  looks empty right after switching `receipts.sink`.
