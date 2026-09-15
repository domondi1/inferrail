import { useState } from "react";

// Same fallback chain the marketing site's own copy button uses
// (docs/index.html): clipboard.writeText can genuinely fail for reasons
// having nothing to do with this being "just a local app" -- a denied
// permission prompt, enterprise policy, an insecure context -- so this
// never assumes success silently.
export function CopyButton({ text }: { text: string }): JSX.Element {
  const [label, setLabel] = useState("Copy");

  async function onClick(): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      setLabel("Copied");
    } catch {
      setLabel("Copy failed");
    }
    setTimeout(() => setLabel("Copy"), 1500);
  }

  return (
    <button className="nav-tab" onClick={() => void onClick()} style={{ fontSize: 10 }}>
      {label}
    </button>
  );
}
