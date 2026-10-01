from importlib.metadata import PackageNotFoundError, version

from inferrail.tracking import (
    attributed_async_http_client,
    attributed_http_client,
    current_task_id,
    track_task,
)

try:
    # pyproject.toml's [project].version is the single source of truth;
    # this reads back whatever was actually installed rather than
    # duplicating that string here, so installed package metadata, FastAPI
    # metadata (gateway/app.py), and the MCP server's declared version
    # (inferrail-mcp/src/inferrail_mcp/server.py) — both of which import
    # __version__ from here — cannot drift from it or each other.
    __version__ = version("inferrail")
except PackageNotFoundError:  # pragma: no cover - only when run unpackaged
    __version__ = "0.0.0+unknown"

__all__ = [
    "__version__",
    "attributed_async_http_client",
    "attributed_http_client",
    "current_task_id",
    "start",
    "stop",
    "track_task",
]


def __getattr__(name: str) -> object:
    # `inferrail.start()` / `inferrail.stop()` (embedded gateway) are
    # imported on first use, so `import inferrail` stays as light as it was.
    if name in ("start", "stop"):
        from inferrail import embedded

        return getattr(embedded, name)
    raise AttributeError(f"module 'inferrail' has no attribute {name!r}")
