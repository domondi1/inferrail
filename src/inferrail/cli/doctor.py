"""`inferrail doctor` — checks port availability, pricing-catalog
freshness, and provider reachability, each with a one-line fix. See
docs/PRODUCT.md.

Provider reachability is a bare TCP connect to the configured
`base_url`'s host:port — never an HTTP request, and never using a
provider's own API key. It confirms network/DNS/firewall reachability
only, the same "is anything listening there" question a human would
answer with `nc -zv`/`telnet`, deliberately short of anything that
could look like an authenticated API call.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from inferrail.cli.pricing import catalog_freshness
from inferrail.config.loader import load_config
from inferrail.errors import ConfigurationError

_PORT_CHECK_TIMEOUT_SECONDS = 1.0
_REACHABILITY_TIMEOUT_SECONDS = 3.0


def _check_port(host: str, port: int) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT_SECONDS)
        in_use = sock.connect_ex((host, port)) == 0
    if in_use:
        return False, (
            f"port {port} on {host} is already in use — stop whatever's using it, "
            f"or run 'inferrail serve --port <other-port>'"
        )
    return True, f"port {port} on {host} is free"


def _check_provider_reachability(name: str, base_url: str) -> tuple[bool, str]:
    parsed = urlparse(base_url)
    host = parsed.hostname
    if host is None:
        return False, f"provider '{name}': could not parse a host out of base_url '{base_url}'"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=_REACHABILITY_TIMEOUT_SECONDS):
            pass
    except OSError as exc:
        return False, (
            f"provider '{name}': cannot reach {host}:{port} ({exc}) — check your "
            f"network connection, DNS, or firewall"
        )
    return True, f"provider '{name}': {host}:{port} is reachable"


def run_doctor(config_path: str) -> int:
    problems = 0

    try:
        config = load_config(config_path)
    except ConfigurationError as exc:
        print(f"config:  FAIL — {exc}")
        print(f"         fix: check {config_path}, or run 'inferrail config check' for detail")
        return 1
    print(f"config:  OK — {config_path}")

    port_ok, port_message = _check_port(config.server.host, config.server.port)
    print(f"port:    {'OK' if port_ok else 'FAIL'} — {port_message}")
    problems += 0 if port_ok else 1

    for name, count, oldest, age_days, is_stale in catalog_freshness():
        if oldest is None:
            continue
        status = "STALE" if is_stale else "OK"
        print(
            f"pricing: {status} — {name} catalog ({count} models), verified "
            f"{oldest.isoformat()} ({age_days} days ago)"
        )
        if is_stale:
            print("         fix: 'pip install --upgrade inferrail', or 'inferrail pricing update'")

    for provider_name, provider_config in config.providers.items():
        try:
            base_url = provider_config.resolved_base_url()
        except ValueError as exc:
            print(f"provider ({provider_name}): FAIL — {exc}")
            problems += 1
            continue
        reachable, message = _check_provider_reachability(provider_name, base_url)
        print(f"provider ({provider_name}): {'OK' if reachable else 'FAIL'} — {message}")
        problems += 0 if reachable else 1

    print()
    if problems:
        print(f"{problems} problem(s) found — see 'fix:' lines above.")
        return 1
    print("No problems found.")
    return 0
