"""Generate the README data-flow diagram and logo tile as SVG.

One layout, two color themes, so the README can serve the right one to
GitHub's light and dark modes through a ``<picture>`` element. The
existing logo (``docs/favicon-192x192.png``) is embedded unchanged as a
data URI; it is placed on a light tile because its navy strokes are not
legible on a dark background.

The diagram shows what the code does (see the README's "How it works"
links): request and response content passes through the gateway to the
configured provider, the gateway writes a metadata receipt to a local
store, reports read that store, and the lifecycle beacon sends only if a
collector endpoint is configured.

Usage::

    python scripts/render_flow_svg.py

Writes ``docs/assets/inferrail-flow-light.svg``,
``docs/assets/inferrail-flow-dark.svg``, and
``docs/assets/inferrail-logo.svg``.
"""

from __future__ import annotations

import base64
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
LOGO_PNG = REPO / "docs" / "favicon-192x192.png"

THEMES = {
    "light": {
        "bg": "#F6F8FA",
        "text": "#1F2328",
        "muted": "#57606A",
        "box": "#FFFFFF",
        "box_stroke": "#D0D7DE",
        "zone": "#021A36",
        "zone_fill": "#EAF2FB",
        "provider_fill": "#F3F4F6",
        "payload": "#0B7285",
        "meta": "#9A6700",
        "cond": "#6E7781",
    },
    "dark": {
        "bg": "#161B22",
        "text": "#E6EDF3",
        "muted": "#9198A1",
        "box": "#0D1117",
        "box_stroke": "#3D444D",
        "zone": "#79C0FF",
        "zone_fill": "#111D2B",
        "provider_fill": "#1C2128",
        "payload": "#39C5D6",
        "meta": "#E3B341",
        "cond": "#9198A1",
    },
}

FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def _logo_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(LOGO_PNG.read_bytes()).decode()


def _text(x: float, y: float, s: str, cls: str, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{s}</text>'


def _box(x: float, y: float, w: float, h: float, cls: str = "box") -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="{cls}"/>'


def _arrow(x1: float, y1: float, x2: float, y2: float, cls: str) -> str:
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}" '
        f'marker-end="url(#m-{cls})"/>'
    )


def flow_svg(theme: str) -> str:
    c = THEMES[theme]
    logo = _logo_data_uri()
    parts: list[str] = []
    add = parts.append

    # Zones: the operator's machine, and the upstream provider.
    add(f'<rect x="1" y="1" width="1038" height="538" rx="16" fill="{c["bg"]}"/>')
    add('<rect x="24" y="24" width="598" height="492" rx="14" class="zone"/>')
    add(_text(44, 52, "YOUR MACHINE (you run and control this)", "zlabel"))
    add('<rect x="716" y="24" width="300" height="252" rx="14" class="pzone"/>')
    add(_text(736, 52, "UPSTREAM PROVIDER", "zlabel"))

    # Application.
    add(_box(48, 96, 200, 108))
    add(_text(148, 126, "Your application", "title", "middle"))
    add(_text(148, 150, "OpenAI or Anthropic SDK,", "small", "middle"))
    add(_text(148, 168, "base_url set to the gateway", "small", "middle"))
    add(_text(148, 190, "optional attribution headers", "small", "middle"))

    # Gateway.
    add(_box(346, 84, 250, 176, "gbox"))
    add('<rect x="358" y="96" width="34" height="34" rx="7" fill="#FFFFFF"/>')
    add(f'<image x="361" y="99" width="28" height="28" href="{logo}"/>')
    add(_text(402, 112, "Inferrail gateway", "title"))
    add(_text(402, 130, "inferrail serve", "mono"))
    add(_text(362, 158, "reads the provider key from its env", "small"))
    add(_text(362, 178, "forwards content in memory", "small"))
    add(_text(362, 198, "measures usage, prices it", "small"))
    add(_text(362, 218, "builds one receipt per request", "small"))
    add(_text(362, 244, "loopback by default: 127.0.0.1:8000", "tiny"))

    # Provider.
    add(_box(740, 84, 252, 170))
    add(_text(866, 114, "OpenAI or Anthropic", "title", "middle"))
    add(_text(866, 132, "(or a compatible endpoint)", "small", "middle"))
    add(_text(866, 164, "receives your request content", "small", "middle"))
    add(_text(866, 184, "and your provider API key", "small", "middle"))
    add(_text(866, 214, "its own retention and", "small", "middle"))
    add(_text(866, 232, "billing policies apply", "small", "middle"))

    # Receipt store and readers.
    add(_box(346, 360, 250, 96))
    add(_text(471, 388, "Local receipt store", "title", "middle"))
    add(_text(471, 410, "JSONL (default) or SQLite", "small", "middle"))
    add(_text(471, 430, "tokens, cost, status, timing,", "small", "middle"))
    add(_text(471, 446, "caller-supplied attributes", "small", "middle"))

    add(_box(48, 360, 200, 96))
    add(_text(148, 390, "Reports", "title", "middle"))
    add(_text(148, 412, "inferrail report / work", "mono", "middle"))
    add(_text(148, 432, "local dashboard, MCP tools", "small", "middle"))

    # Payload path: application <-> gateway <-> provider.
    add(_arrow(250, 128, 342, 128, "payload"))
    add(_text(296, 120, "request", "plabel", "middle"))
    add(_arrow(344, 166, 252, 166, "payload"))
    add(_text(298, 184, "response", "plabel", "middle"))
    add(_arrow(598, 128, 736, 128, "payload"))
    add(_text(669, 118, "request +", "plabel", "middle"))
    add(_text(669, 146, "API key", "plabel", "middle"))
    add(_arrow(738, 190, 600, 190, "payload"))
    add(_text(669, 180, "response +", "plabel", "middle"))
    add(_text(669, 208, "usage", "plabel", "middle"))

    # Metadata path: gateway -> store -> reports.
    add(_arrow(471, 262, 471, 356, "meta"))
    add(_text(482, 292, "receipt metadata", "mlabel"))
    add(_text(482, 310, "no message bodies", "mlabel"))
    add(_arrow(344, 408, 252, 408, "meta"))
    add(_text(298, 398, "read", "mlabel", "middle"))

    # Conditional beacon.
    add('<rect x="740" y="340" width="252" height="116" rx="10" class="cbox"/>')
    add(_text(866, 368, "Usage beacon collector", "title", "middle"))
    add(_text(866, 390, "sends only if usage_ping.endpoint", "small", "middle"))
    add(_text(866, 408, "is set (no default endpoint)", "small", "middle"))
    add(_text(866, 432, "install id, event, version,", "small", "middle"))
    add(_text(866, 448, "OS, Python. No traffic data.", "small", "middle"))
    add('<path d="M598 236 C 670 236, 670 398, 736 398" class="cond" fill="none" '
        'marker-end="url(#m-cond)"/>')

    # Legend.
    add(_arrow(44, 492, 84, 492, "payload"))
    add(_text(92, 497, "request/response content", "legend"))
    add(_arrow(262, 492, 302, 492, "meta"))
    add(_text(310, 497, "metadata only", "legend"))
    add(_arrow(420, 492, 460, 492, "cond"))
    add(_text(468, 497, "conditional", "legend"))
    add(_text(740, 500, "Receipt sinks write only to", "tiny"))
    add(_text(740, 516, "local files on this machine.", "tiny"))

    markers = "".join(
        f'<marker id="m-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" '
        f'fill="{c[name]}"/></marker>'
        for name in ("payload", "meta", "cond")
    )
    style = f"""
    text {{ font-family: {FONT}; fill: {c["text"]}; }}
    .title {{ font-size: 15px; font-weight: 600; }}
    .small {{ font-size: 12.5px; fill: {c["muted"]}; }}
    .tiny {{ font-size: 11.5px; fill: {c["muted"]}; }}
    .mono {{ font-family: {MONO}; font-size: 12.5px; fill: {c["muted"]}; }}
    .zlabel {{ font-size: 11.5px; font-weight: 700; letter-spacing: .08em; fill: {c["zone"]}; }}
    .legend {{ font-size: 12px; fill: {c["muted"]}; }}
    .plabel {{ font-size: 12px; font-weight: 600; fill: {c["payload"]}; }}
    .mlabel {{ font-size: 12px; font-weight: 600; fill: {c["meta"]}; }}
    .box {{ fill: {c["box"]}; stroke: {c["box_stroke"]}; stroke-width: 1.2; }}
    .gbox {{ fill: {c["box"]}; stroke: {c["zone"]}; stroke-width: 1.8; }}
    .cbox {{ fill: {c["box"]}; stroke: {c["cond"]}; stroke-width: 1.2; stroke-dasharray: 2 4; }}
    .zone {{ fill: {c["zone_fill"]}; stroke: {c["zone"]}; stroke-width: 1.5;
             stroke-dasharray: 8 5; }}
    .pzone {{ fill: {c["provider_fill"]}; stroke: {c["box_stroke"]}; stroke-width: 1.5; }}
    .payload {{ stroke: {c["payload"]}; stroke-width: 2.2; }}
    .meta {{ stroke: {c["meta"]}; stroke-width: 2; stroke-dasharray: 7 4; }}
    .cond {{ stroke: {c["cond"]}; stroke-width: 1.6; stroke-dasharray: 2 4; }}
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1040 540" width="1040" '
        'height="540" role="img" aria-labelledby="t d">'
        '<title id="t">Inferrail self-hosted data flow</title>'
        '<desc id="d">Your application sends requests to the Inferrail gateway on your '
        "machine. The gateway reads the provider API key from its environment and forwards "
        "request content and the key to OpenAI or Anthropic, then returns the response. "
        "Separately it writes a metadata receipt (tokens, cost, status, timing, attributes, "
        "no message bodies) to a local JSONL or SQLite store that reports read. A usage "
        "beacon sends lifecycle events only if a collector endpoint is configured.</desc>"
        f"<defs><style>{style}</style>{markers}</defs>" + "".join(parts) + "</svg>\n"
    )


def logo_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" '
        'height="120" role="img" aria-label="Inferrail logo">'
        '<rect width="120" height="120" rx="26" fill="#FFFFFF"/>'
        f'<image x="14" y="14" width="92" height="92" href="{_logo_data_uri()}"/>'
        "</svg>\n"
    )


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        (ASSETS / f"inferrail-flow-{theme}.svg").write_text(flow_svg(theme))
    (ASSETS / "inferrail-logo.svg").write_text(logo_svg())
    print("wrote docs/assets/inferrail-flow-{light,dark}.svg and docs/assets/inferrail-logo.svg")


if __name__ == "__main__":
    main()
