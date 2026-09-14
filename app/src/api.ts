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
