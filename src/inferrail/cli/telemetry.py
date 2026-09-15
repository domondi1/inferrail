"""`inferrail telemetry preview|status|enable|disable` — the CLI half of
the opt-in usage ping (docs/adr/0019-opt-in-usage-ping.md), independent
of the dashboard so a CLI-only user can inspect and control it without
ever running `--app-mode`.

`preview` in particular exists so nobody has to trust this project's own
claim about what the ping sends — it prints the exact JSON body for
every lifecycle event, built from this install's real id/OS/version,
without sending anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from inferrail.appdata import ensure_app_data_dir
from inferrail.config.loader import load_config
from inferrail.config.models import UsagePingConfig
from inferrail.errors import ConfigurationError
from inferrail.usage_ping.install_id import ensure_install_id
from inferrail.usage_ping.payload import build_payload
from inferrail.usage_ping.state import KNOWN_EVENTS, load_state, save_state

_PRIVACY_URL = "https://github.com/domondi1/inferrail/blob/main/docs/privacy/usage-ping.md"


def _load_usage_ping_config(config_path: str | None) -> UsagePingConfig:
    """Permissive by design, unlike `inferrail doctor`'s own config
    loading: `inferrail telemetry ...` must work with no `inferrail.yaml`
    at all (e.g. right after `inferrail demo`, before any config exists)
    -- an unconfigured endpoint is exactly the normal, expected state
    this command is meant to report honestly, not an error."""
    path = Path(config_path or "inferrail.yaml")
    if not path.exists():
        return UsagePingConfig()
    try:
        return load_config(str(path)).usage_ping
    except ConfigurationError:
        return UsagePingConfig()


def _print_status(app_data: Path, config: UsagePingConfig) -> None:
    state = load_state(app_data, default_enabled=config.enabled)
    print(f"  enabled:    {state.enabled}")
    if config.endpoint:
        print(f"  endpoint:   {config.endpoint}")
        print(f"  active:     {state.enabled}")
    else:
        print("  endpoint:   (not configured)")
        print("  active:     False — not yet active, no collection endpoint is configured")
    print(f"  install id: {ensure_install_id(app_data)}")
    print(f"  privacy:    {_PRIVACY_URL}")


def run_telemetry_status(config_path: str | None) -> int:
    app_data = ensure_app_data_dir()
    config = _load_usage_ping_config(config_path)
    print("Usage ping status:")
    _print_status(app_data, config)
    return 0


def run_telemetry_preview(config_path: str | None) -> int:
    app_data = ensure_app_data_dir()
    config = _load_usage_ping_config(config_path)
    install_id = ensure_install_id(app_data)
    print("Usage ping status:")
    _print_status(app_data, config)
    print()
    print("Exact payload for each lifecycle event — nothing else is ever sent,")
    print("and none of these are being sent right now, only shown:")
    for event in KNOWN_EVENTS:
        print(f"  {event}:")
        print("    " + json.dumps(build_payload(event, install_id)))
    return 0


def run_telemetry_enable(config_path: str | None) -> int:
    app_data = ensure_app_data_dir()
    config = _load_usage_ping_config(config_path)
    state = load_state(app_data, default_enabled=config.enabled)
    state.enabled = True
    save_state(app_data, state)
    print("Usage ping enabled.")
    if not config.endpoint:
        print(
            "Note: no usage_ping.endpoint is configured in inferrail.yaml, so this is "
            "still inert — nothing will actually be sent until one is."
        )
    return 0


def run_telemetry_disable(config_path: str | None) -> int:
    app_data = ensure_app_data_dir()
    config = _load_usage_ping_config(config_path)
    state = load_state(app_data, default_enabled=config.enabled)
    state.enabled = False
    save_state(app_data, state)
    print("Usage ping disabled.")
    return 0
