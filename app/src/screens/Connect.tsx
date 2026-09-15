import { CopyButton } from "../CopyButton";

// The dashboard is served by the exact same process as the gateway
// (docs/adr/0017), so window.location.origin IS the real, currently
// running gateway base URL -- these snippets are copy-paste-correct for
// this install, not a generic 127.0.0.1:8000 placeholder that might not
// match the actual port.
const BASE_URL = typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:8000";

interface Snippet {
  title: string;
  blurb: string;
  code: string;
}

function snippets(base: string): Snippet[] {
  return [
    {
      title: "curl",
      blurb: "Any OpenAI-compatible client, or a quick check from a terminal.",
      code: `curl ${base}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "X-Inferrail-Attribute-Customer: acme" \\
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'`,
    },
    {
      title: "Claude Code / any Anthropic SDK client",
      blurb:
        "Set this before launching the tool -- the SDKs' own base-URL env var. " +
        "Needs an anthropic route configured in inferrail.yaml first.",
      code: `export ANTHROPIC_BASE_URL=${base}`,
    },
    {
      title: "Anthropic Messages API (curl)",
      blurb:
        "Real streaming + tool use, priced via the catalog -- not a translated request. " +
        "Needs an anthropic route named 'claude' configured in inferrail.yaml first.",
      code: `curl ${base}/v1/messages \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "claude",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'`,
    },
    {
      title: "OpenAI Python SDK",
      blurb: "Point base_url at this install; the api_key can be any non-empty string.",
      code: `from openai import OpenAI

client = OpenAI(base_url="${base}/v1", api_key="not-needed")`,
    },
    {
      title: "LangChain",
      blurb: "",
      code: `from langchain_openai import ChatOpenAI

llm = ChatOpenAI(base_url="${base}/v1", api_key="not-needed", model="default")`,
    },
  ];
}

export function Connect(): JSX.Element {
  return (
    <div>
      <h1 className="screen-title">Connect</h1>
      <p className="screen-subtitle">
        Point the tool you already use at this running install — every request becomes an
        attributed, payload-free receipt.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {snippets(BASE_URL).map((s) => (
          <div key={s.title}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <strong style={{ fontFamily: "var(--font-label)", fontSize: 13 }}>{s.title}</strong>
              <CopyButton text={s.code} />
            </div>
            {s.blurb && <p className="receipt-attrs" style={{ margin: "4px 0" }}>{s.blurb}</p>}
            <pre
              style={{
                background: "var(--ink)",
                color: "var(--paper)",
                padding: 12,
                overflowX: "auto",
                fontSize: 12,
                margin: "6px 0 0",
              }}
            >
              {s.code}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
