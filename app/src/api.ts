// Local control API client (docs/adr/0016, docs/adr/0017). Every call is
// same-origin against the FastAPI process that also served this page --
// there is no base URL to configure.

/** A receipt as returned by `GET /v1/local/receipts` and streamed by
 * `GET /v1/local/stream` -- kept intentionally loose (not the full
 * `InferenceReceipt` pydantic shape) so the frontend doesn't need to be
 * rebuilt every time the backend schema gains an unrelated field. Cost
 * fields are `string | null` on the wire (Decimal serializes as a JSON
 * string) -- `null` means genuinely unknown, and must never be rendered
 * as "$0" (docs/PRODUCT.md's "honest numbers" rule, MISSION.md's
 * non-negotiables).
 */
export interface Receipt {
  receipt_id: string;
  timestamp: string;
  route: string;
  provider: string;
  model: string;
  status: "success" | "error" | "partial";
  estimated_cost_usd: string | null;
  attributes: Record<string, string>;
}

/** Read once at load time from the URL the CLI printed
 * (`http://host:port/dashboard/?token=...`) and held only in memory for
 * the life of the tab -- never `localStorage` (docs/adr/0017). */
export function getToken(): string {
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export function hasToken(): boolean {
  return getToken().length > 0;
}

/** A work_id rollup as returned by `GET /v1/local/work` and
 * `GET /v1/local/work/{work_id}` -- see `inferrail.work.schema.WorkSummary`.
 * `known_attributed_inference_cost_usd: null` means genuinely unknown
 * (no priced receipt exists yet), distinct from a real `"0"`. */
export interface WorkSummary {
  work_id: string;
  outcome_status: string | null;
  outcome_recorded_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  receipt_count: number;
  known_attributed_inference_cost_usd: string | null;
  unknown_cost_count: number;
  inference_status: "success" | "error" | "partial" | "unknown";
}

export class LocalApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** A plain fetch() against the local API, authenticated via the
 * Authorization header (unlike the SSE stream, fetch() can set headers
 * normally, so this is the ordinary case, not the exception). */
async function fetchLocal<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok) {
    throw new LocalApiError(response.status, `${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

export function listWork(): Promise<WorkSummary[]> {
  return fetchLocal<WorkSummary[]>("/v1/local/work");
}

export function getWork(workId: string): Promise<WorkSummary> {
  return fetchLocal<WorkSummary>(`/v1/local/work/${encodeURIComponent(workId)}`);
}

/** See `inferrail.budgets.schema.Budget`. */
export type BudgetScope = "global" | "project" | "work_id";
export type BudgetWindow = "per_work" | "daily" | "monthly";
export type BudgetMode = "warn" | "block";

export interface Budget {
  budget_id: string;
  scope: BudgetScope;
  scope_value: string | null;
  window: BudgetWindow;
  mode: BudgetMode;
  limit_usd: string;
  created_at: string;
}

export interface BudgetCreate {
  scope: BudgetScope;
  scope_value?: string | null;
  window: BudgetWindow;
  mode: BudgetMode;
  limit_usd: string;
}

/** See `localapi.schemas.BudgetSpend` -- `spent_usd`/`limit_usd` reuse
 * the exact computation `BudgetEnforcer.check` itself uses, never a
 * client-side re-derivation. `has_unpriced_usage: true` means this
 * budget's spend is a floor, not the true total. */
export interface BudgetSpend {
  budget_id: string;
  limit_usd: string;
  spent_usd: string;
  has_unpriced_usage: boolean;
}

export function listBudgets(): Promise<Budget[]> {
  return fetchLocal<Budget[]>("/v1/local/budgets");
}

export function listBudgetSpend(): Promise<BudgetSpend[]> {
  return fetchLocal<BudgetSpend[]>("/v1/local/budgets/spend");
}

export async function createBudget(payload: BudgetCreate): Promise<Budget> {
  const response = await fetch("/v1/local/budgets", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${getToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new LocalApiError(response.status, body.detail ?? `create budget -> ${response.status}`);
  }
  return (await response.json()) as Budget;
}

export async function deleteBudget(budgetId: string): Promise<void> {
  const response = await fetch(`/v1/local/budgets/${encodeURIComponent(budgetId)}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok && response.status !== 404) {
    throw new LocalApiError(response.status, `delete budget -> ${response.status}`);
  }
}

/** The blocked-request log: every receipt a real budget block produced
 * (`status: "error"` + a `budget_id` attribute -- see
 * `budgets.enforcement.augment_attributes_with_block`), newest first.
 * Fetches one page of error receipts and filters client-side for the
 * `budget_id` marker, since not every `status: "error"` receipt is a
 * budget block (a provider failure looks the same otherwise). */
export async function listBlockedReceipts(limit = 100): Promise<Receipt[]> {
  const page = await fetchLocal<{ receipts: Receipt[] }>(
    `/v1/local/receipts?status=error&limit=${limit}`,
  );
  return page.receipts.filter((r) => Boolean(r.attributes.budget_id)).reverse();
}

/** Opens the SSE tail of newly-emitted receipts. Native `EventSource`
 * cannot set an `Authorization` header, so the token travels as a query
 * parameter here -- the one deliberate, documented exception in
 * docs/adr/0017's auth section. */
export function streamReceipts(
  onReceipt: (receipt: Receipt) => void,
  onStatusChange: (status: "connecting" | "connected" | "error") => void,
): () => void {
  const token = getToken();
  const source = new EventSource(`/v1/local/stream?token=${encodeURIComponent(token)}`);

  onStatusChange("connecting");
  source.onopen = () => onStatusChange("connected");
  source.onerror = () => onStatusChange("error");
  source.onmessage = (event) => {
    try {
      onReceipt(JSON.parse(event.data) as Receipt);
    } catch {
      // A malformed event is dropped, not shown -- there is nothing
      // useful to render for it, and it must not crash the live feed.
    }
  };

  return () => source.close();
}

/** One row of the AP recovery report (`ap.report.LiveReportRow`), kept
 * loose like `Receipt` -- only the fields the Recover screen actually
 * renders, not the full report contract. Cost fields are `string | null`
 * on the wire; `null` is genuinely unknown, never `$0`. */
export interface PendingReview {
  work_id: string;
  failure_type: string;
  recommended_action: string;
  reason: string;
  status: string;
  retry_status: string | null;
  sunk_cost_usd: string | null;
  retry_cost_usd: string | null;
  validation_passed: boolean | null;
  handoff_ref: string | null;
}

export async function listPendingReviews(): Promise<PendingReview[]> {
  const page = await fetchLocal<{ rows: PendingReview[] }>("/v1/local/ap/pending");
  return page.rows;
}

export interface OutcomeRequest {
  outcome: string;
  source?: string;
  correction_delta_usd?: string | null;
  review_cost_usd?: string | null;
}

export async function recordOutcome(workId: string, payload: OutcomeRequest): Promise<void> {
  const response = await fetch(`/v1/local/ap/${encodeURIComponent(workId)}/outcome`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${getToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new LocalApiError(
      response.status,
      body.detail ?? `record outcome -> ${response.status}`,
    );
  }
}

export interface CatalogFreshness {
  name: string;
  model_count: number;
  oldest_verified_date: string | null;
  age_days: number | null;
  is_stale: boolean;
}

export async function getPricingFreshness(): Promise<CatalogFreshness[]> {
  const page = await fetchLocal<{ catalogs: CatalogFreshness[] }>("/v1/local/pricing/freshness");
  return page.catalogs;
}

/** Triggers a browser download of every stored receipt as JSONL. Can't
 * use a plain `<a href>` (the local API requires a bearer token no
 * plain link can carry) -- fetches the file, then hands the browser a
 * blob URL to save, same trick `docs/index.html`'s own download flows
 * use nowhere yet but is the standard workaround for an authenticated
 * download. */
export async function downloadReceiptsExport(): Promise<void> {
  const response = await fetch("/v1/local/receipts/export", {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok) {
    throw new LocalApiError(response.status, `export -> ${response.status}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "inferrail-receipts.jsonl";
  a.click();
  URL.revokeObjectURL(url);
}
