from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from inferrail.config.models import InferrailConfig
from inferrail.errors import ConfigurationError


def load_config(path: str | Path) -> InferrailConfig:
    """Load and validate an ``inferrail.yaml`` file.

    Raises :class:`ConfigurationError` with a human-readable message on any
    failure — missing file, invalid YAML, or a schema/cross-reference
    violation. This function never reads environment variables; secret
    resolution happens later, in ``inferrail.providers.registry``, so that
    config *shape* can be validated even before secrets are exported.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigurationError(
            f"config file not found: {config_path}\n"
            f"Copy inferrail.example.yaml to {config_path.name} to get started."
        )

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{config_path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigurationError(f"{config_path} is empty")
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{config_path} must contain a YAML mapping at the top level, "
            f"got {type(raw).__name__}"
        )

    try:
        return InferrailConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(
            f"{config_path} failed validation:\n{_format_validation_error(exc)}"
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
