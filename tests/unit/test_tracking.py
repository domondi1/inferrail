from __future__ import annotations

import asyncio

import httpx
import pytest

from inferrail.tracking import (
    attributed_async_http_client,
    attributed_http_client,
    current_task_id,
    inject_task_id_header,
    inject_task_id_header_async,
    track_task,
)

_HEADER = "X-Inferrail-Attribute-Task-Id"
_BASE_URL = "http://example.invalid"


def _request(url: str = "http://example.invalid/v1/chat/completions") -> httpx.Request:
    return httpx.Request("POST", url)


# ---------------------------------------------------------------------------
# track_task / current_task_id: contextvar semantics
# ---------------------------------------------------------------------------


def test_current_task_id_is_none_outside_track_task() -> None:
    assert current_task_id() is None


def test_track_task_sets_and_restores_current_task_id() -> None:
    assert current_task_id() is None
    with track_task("bug_9281"):
        assert current_task_id() == "bug_9281"
    assert current_task_id() is None


def test_track_task_nesting_restores_outer_value() -> None:
    with track_task("outer"):
        with track_task("inner"):
            assert current_task_id() == "inner"
        assert current_task_id() == "outer"
    assert current_task_id() is None


def test_track_task_restores_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with track_task("bug_9281"):
            raise RuntimeError("boom")
    assert current_task_id() is None


def test_track_task_rejects_empty_task_id() -> None:
    with pytest.raises(ValueError):
        with track_task(""):
            pass


def test_track_task_usable_as_decorator() -> None:
    seen: list[str | None] = []

    @track_task("bug_9281")
    def do_work() -> None:
        seen.append(current_task_id())

    do_work()

    assert seen == ["bug_9281"]
    assert current_task_id() is None


def test_track_task_propagates_through_nested_function_with_no_parameter() -> None:
    def call_model() -> str | None:
        return current_task_id()

    def run_subagent() -> str | None:
        # No task_id parameter anywhere in this signature.
        return call_model()

    with track_task("bug_9281"):
        assert run_subagent() == "bug_9281"


async def _tagged_leaf(name: str) -> str | None:
    await asyncio.sleep(0)
    return current_task_id()


async def _concurrent_task(name: str) -> str | None:
    with track_task(name):
        await asyncio.sleep(0)
        return await _tagged_leaf(name)


def test_track_task_isolates_concurrent_asyncio_tasks() -> None:
    async def run() -> tuple[str | None, str | None]:
        return await asyncio.gather(
            _concurrent_task("task_a"),
            _concurrent_task("task_b"),
        )

    result_a, result_b = asyncio.run(run())

    assert result_a == "task_a"
    assert result_b == "task_b"
    assert current_task_id() is None


# ---------------------------------------------------------------------------
# httpx event hooks
# ---------------------------------------------------------------------------


def test_inject_task_id_header_sets_header_when_tracking() -> None:
    request = _request()
    with track_task("bug_9281"):
        inject_task_id_header(request, allowed_base_url=_BASE_URL)
    assert request.headers[_HEADER] == "bug_9281"


def test_inject_task_id_header_no_op_outside_track_task() -> None:
    request = _request()
    inject_task_id_header(request, allowed_base_url=_BASE_URL)
    assert _HEADER not in request.headers


def test_inject_task_id_header_async_matches_sync_behavior() -> None:
    request = _request()
    with track_task("bug_9281"):
        asyncio.run(inject_task_id_header_async(request, allowed_base_url=_BASE_URL))
    assert request.headers[_HEADER] == "bug_9281"


def test_inject_task_id_header_omitted_for_unrelated_destination() -> None:
    # The core leak this guards against: the same tracked context, but the
    # request is going somewhere other than allowed_base_url (a caller
    # reusing the client against a real, non-Inferrail provider endpoint).
    request = _request("https://api.some-other-provider.example/v1/chat/completions")
    with track_task("bug_9281"):
        inject_task_id_header(request, allowed_base_url=_BASE_URL)
    assert _HEADER not in request.headers


def test_inject_task_id_header_async_omitted_for_unrelated_destination() -> None:
    request = _request("https://api.some-other-provider.example/v1/chat/completions")
    with track_task("bug_9281"):
        asyncio.run(inject_task_id_header_async(request, allowed_base_url=_BASE_URL))
    assert _HEADER not in request.headers


# ---------------------------------------------------------------------------
# attributed_http_client / attributed_async_http_client: end-to-end via a
# real httpx transport (MockTransport), no network
# ---------------------------------------------------------------------------


def test_attributed_http_client_tags_outgoing_requests() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = attributed_http_client(base_url=_BASE_URL)
    client._transport = httpx.MockTransport(handler)

    with track_task("bug_9281"):
        client.get("http://example.invalid/v1/chat/completions")

    assert captured[0].headers[_HEADER] == "bug_9281"


def test_attributed_http_client_omits_header_when_not_tracking() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = attributed_http_client(base_url=_BASE_URL)
    client._transport = httpx.MockTransport(handler)

    client.get("http://example.invalid/v1/chat/completions")

    assert _HEADER not in captured[0].headers


def test_attributed_http_client_omits_header_for_unrelated_destination() -> None:
    # The client is only ever configured with one base_url in practice
    # (mirroring real SDK usage — see README's example), but a caller
    # could still pass an absolute URL elsewhere through the same client
    # instance; that must never carry the task id either.
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = attributed_http_client(base_url=_BASE_URL)
    client._transport = httpx.MockTransport(handler)

    with track_task("bug_9281"):
        client.get("https://api.some-other-provider.example/v1/chat/completions")

    assert _HEADER not in captured[0].headers


def test_attributed_async_http_client_tags_outgoing_requests() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        client = attributed_async_http_client(base_url=_BASE_URL)
        client._transport = httpx.MockTransport(handler)
        with track_task("bug_9281"):
            await client.get("http://example.invalid/v1/chat/completions")

    asyncio.run(run())

    assert captured[0].headers[_HEADER] == "bug_9281"


def test_attributed_async_http_client_omits_header_for_unrelated_destination() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        client = attributed_async_http_client(base_url=_BASE_URL)
        client._transport = httpx.MockTransport(handler)
        with track_task("bug_9281"):
            await client.get("https://api.some-other-provider.example/v1/chat/completions")

    asyncio.run(run())

    assert _HEADER not in captured[0].headers


def test_attributed_async_http_client_isolates_concurrent_tasks() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async def call_tagged(client: httpx.AsyncClient, name: str) -> None:
        with track_task(name):
            await client.get("http://example.invalid/v1/chat/completions")

    async def run() -> None:
        client = attributed_async_http_client(base_url=_BASE_URL)
        client._transport = httpx.MockTransport(handler)
        await asyncio.gather(
            call_tagged(client, "task_a"),
            call_tagged(client, "task_b"),
        )

    asyncio.run(run())

    tagged = {r.headers[_HEADER] for r in captured}
    assert tagged == {"task_a", "task_b"}
