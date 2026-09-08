"""Real A2A transport helper for Phase B tests.

Unlike `_a2a_economic_authority_crash_helper.py` (a subprocess entry point),
this module is imported directly by the test file. It builds a real
`a2a-sdk` `Client` against a real running server subprocess -- there is no
hand-rolled JSON-RPC here; every request goes through the actual installed
SDK's `ClientFactory`/`AuthInterceptor`, the same as a genuinely independent
caller would use.

Not a test module itself (leading underscore keeps pytest from collecting
it).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from a2a.client import AuthInterceptor, ClientConfig, ClientFactory, InMemoryContextCredentialStore
from a2a.client.client import Client, ClientCallContext
from a2a.helpers import new_data_message
from a2a.types import Role, SendMessageRequest, Task

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
SERVER_SCRIPT = HOSTED_DIR / "server.py"
BEARER_SCHEME = "capabilityBearer"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def agent_process(
    port: int, db_path: Path, capability_db_path: Path, log_path: Path | None = None
) -> Iterator[str]:
    """Starts a real server subprocess and yields its base URL once ready.

    Logs to `log_path` (a real file, not a pipe) so a test can read the
    server's log output at any point without risking a pipe-buffer
    deadlock -- used by tests that must prove a credential never appears
    in server logs.
    """
    args = [
        sys.executable,
        str(SERVER_SCRIPT),
        "--port",
        str(port),
        "--db-path",
        str(db_path),
        "--capability-db-path",
        str(capability_db_path),
    ]
    log_path = log_path or (db_path.parent / f"server-{port}.log")
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, text=True)
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_ready(port, proc)
            yield base_url
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _wait_for_ready(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/.well-known/agent-card.json"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"agent process exited early (code {proc.returncode})")
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.1)
    raise TimeoutError(f"agent on port {port} did not become ready: {last_error}")


def wait_until_dead(proc: subprocess.Popen, timeout: float = 10.0) -> int:
    """Block until a killed process actually exits; return its exit code."""
    return proc.wait(timeout=timeout)


def kill_hard(proc: subprocess.Popen) -> None:
    """Uncontrolled kill (SIGKILL) -- for real process-restart durability tests."""
    proc.kill()


async def make_client(base_url: str, token: str | None, session_id: str = "sess") -> Client:
    """Builds a real `a2a-sdk` Client. `token`, if given, is delivered only
    via the client's `AuthInterceptor` -- i.e. only ever as the real HTTP
    `Authorization` header, never as part of any message we construct."""
    interceptors = []
    if token is not None:
        cred_store = InMemoryContextCredentialStore()
        await cred_store.set_credentials(session_id, BEARER_SCHEME, token)
        interceptors.append(AuthInterceptor(cred_store))
    factory = ClientFactory(ClientConfig(streaming=False))
    return await factory.create_from_url(base_url, interceptors=interceptors)


def call_context(session_id: str = "sess") -> ClientCallContext:
    return ClientCallContext(state={"sessionId": session_id})


async def send(
    client: Client, ctx: ClientCallContext, payload: dict[str, Any], task_id: str | None = None
) -> Task:
    """Sends one op and returns the resulting Task (never a bare Message,
    since every op here always produces a Task)."""
    message = new_data_message(payload, task_id=task_id, role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)
    async for response in client.send_message(request, context=ctx):
        if response.HasField("task"):
            return response.task
        raise AssertionError(f"expected a Task response, got a bare Message: {response}")
    raise AssertionError("client.send_message yielded no response")


def claim_credential(base_url: str, claim_id: str, bearer_token: str) -> httpx.Response:
    """Plain HTTP call to the out-of-band claim endpoint -- deliberately not
    routed through the A2A client, since it is not an A2A operation."""
    return httpx.post(
        f"{base_url}/capabilities/claim",
        json={"claim_id": claim_id},
        headers={"Authorization": f"Bearer {bearer_token}"},
        timeout=10.0,
    )
