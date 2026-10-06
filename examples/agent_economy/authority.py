"""A self-hosted economic authority for one unit of agent work.

The agent never holds a payment key. This runtime holds the payer key and
the model-provider key, and every economic action an agent asks for goes
through the same sequence:

    reserve the cost against the caller's authority  (atomic; refusal here = nothing signed)
    record the intent durably
    sign / call
    reconcile against the chain                      (not against the seller's word)
    consume the actual cost, release the rest

Ledger: `hosted/a2a_economic_authority/core.py`, unchanged. Each economic
action is a leaf delegation reserved under the caller's delegation, so a
parent's headroom can't be double-allocated however many agents act at
once, and a sub-agent can never spend more than it was delegated.

What this does NOT guarantee is in README.md ("What is and isn't
guaranteed"). In short: the bound holds as long as this process and its
key are not compromised, and the payer account's balance is the hard
backstop if they are.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import sys
import time
from base64 import b64decode
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from x402.client import x402ClientSync
from x402.http.utils import encode_payment_signature_header
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentRequired

_CORE_DIR = Path(__file__).resolve().parents[2] / "hosted" / "a2a_economic_authority"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
from core import EconomicAuthorityStore  # noqa: E402

USDC_DECIMALS = 6
NETWORK = "eip155:84532"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, delegation_id TEXT NOT NULL, work_id TEXT NOT NULL,
    agent_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, delegation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, kind TEXT NOT NULL, resource TEXT NOT NULL,
    pay_to TEXT, amount_usd TEXT NOT NULL, actual_usd TEXT, state TEXT NOT NULL,
    reason TEXT, nonce TEXT, valid_before INTEGER, payload_sha256 TEXT, tx TEXT,
    delivered INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

# x402 action states: RESERVING -> REFUSED | AUTHORIZED -> SETTLED | EXPIRED
# model calls: RESERVING -> CALLING -> SETTLED
# reconcile: RESERVING -> ABANDONING -> ABANDONED (nothing was signed or called);
#            CALLING -> SETTLED at the reserved ceiling (the provider may have billed)
# Leaving RESERVING is a compare-and-set, so a reconcile in another process can't
# release a reservation that this process is about to sign or spend against.
TERMINAL = {"REFUSED", "SETTLED", "EXPIRED", "ABANDONED"}


class Refused(Exception):
    """An economic action was refused before any payment was authorized."""

    def __init__(self, reason: str, detail: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ChainReader(Protocol):
    def authorization_state(self, authorizer: str, nonce: str) -> bool: ...
    def used_event(self, authorizer: str, nonce: str) -> Any: ...


class HttpClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass
class ModelProvider:
    """A provider call made with a key only the runtime holds.

    `complete(messages, max_tokens) -> (text, prompt_tokens, completion_tokens)`.
    Prices are operator-declared per token, as in the gateway's pricing config.
    """

    complete: Callable[[list[dict[str, str]], int], tuple[str, int, int]]
    input_usd_per_token: Decimal
    output_usd_per_token: Decimal
    name: str = "model"


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    return sum(len(m.get("content", "")) for m in messages) // 4 + 4 * len(messages)


def _usd(atomic: int | str) -> Decimal:
    return Decimal(int(atomic)).scaleb(-USDC_DECIMALS)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthorityRuntime:
    def __init__(
        self,
        state_dir: Path,
        *,
        payer: Any,
        chain: ChainReader,
        http: HttpClient,
        provider: ModelProvider | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._ledger = EconomicAuthorityStore(state_dir / "ledger.sqlite3")
        self._db = state_dir / "actions.sqlite3"
        self._payer = payer  # never returned, logged, or sent anywhere but into a signature
        self._chain = chain
        self._http = http
        self._provider = provider
        self._clock = clock
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- storage --------------------------------------------------------

    @contextmanager
    def _conn(self) -> Any:
        conn = sqlite3.connect(self._db, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def _write(self, sql: str, args: tuple[Any, ...]) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, args)
            conn.execute("COMMIT")

    def _transition(self, action_id: str, expected: str, **fields: Any) -> bool:
        """Update the action only if it is still in `expected`; True if it was."""
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE actions SET {cols}, updated_at = ? WHERE action_id = ? AND state = ?",
                (*fields.values(), self._clock(), action_id, expected),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1

    def _set(self, action_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._write(
            f"UPDATE actions SET {cols}, updated_at = ? WHERE action_id = ?",
            (*fields.values(), self._clock(), action_id),
        )

    def _caller(self, token: str) -> sqlite3.Row:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
        if row is None:
            raise PermissionError("unknown or revoked authority token")
        return row

    def _mint_token(self, delegation_id: str, work_id: str, agent_id: str) -> str:
        token = "auth_" + secrets.token_urlsafe(24)
        self._write(
            "INSERT INTO tokens VALUES (?, ?, ?, ?)",
            (_hash(token), delegation_id, work_id, agent_id),
        )
        return token

    def _remaining(self, delegation_id: str) -> Decimal:
        state = self._ledger.get(delegation_id)
        return state.active_reservation_usd if state and state.active else Decimal("0")

    # -- authority ------------------------------------------------------

    def open_work(self, work_id: str, budget_usd: Decimal, agent_id: str = "owner") -> str:
        """Operator-only: the root authority for one unit of work."""
        root = f"work:{work_id}"
        self._ledger.create_root(f"{root}:open", root, agent_id, budget_usd)
        return self._mint_token(root, work_id, agent_id)

    def delegate(self, token: str, agent_id: str, max_usd: Decimal) -> str:
        caller = self._caller(token)
        child = f"{caller['delegation_id']}/{agent_id}"
        outcome = self._ledger.reserve(
            f"{child}:delegate", caller["delegation_id"], child, agent_id, max_usd
        )
        if outcome == "rejected":
            raise Refused(
                "insufficient_authority",
                {
                    "requested_usd": str(max_usd),
                    "remaining_usd": str(self._remaining(caller["delegation_id"])),
                },
            )
        return self._mint_token(child, caller["work_id"], agent_id)

    def finish(self, token: str) -> None:
        """A sub-agent is done: its unspent authority returns to its parent."""
        caller = self._caller(token)
        self._ledger.settle(
            f"{caller['delegation_id']}:finish", caller["delegation_id"], "finished"
        )
        self._write("DELETE FROM tokens WHERE token_hash = ?", (_hash(token),))

    def status(self, token: str) -> dict[str, str]:
        caller = self._caller(token)
        state = self._ledger.get(caller["delegation_id"])
        assert state is not None
        return {
            "delegation_id": state.delegation_id,
            "authority_usd": str(state.authority_usd),
            "remaining_usd": str(self._remaining(state.delegation_id)),
        }

    # -- the one way an action gets admitted ----------------------------

    def _admit(
        self, caller: sqlite3.Row, kind: str, resource: str, amount: Decimal, pay_to: str | None
    ) -> str:
        action_id = f"{kind}-{secrets.token_hex(8)}"
        now = self._clock()
        self._write(
            "INSERT INTO actions (action_id, work_id, delegation_id, agent_id, kind, resource, "
            "pay_to, amount_usd, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESERVING', ?, ?)",
            (
                action_id,
                caller["work_id"],
                caller["delegation_id"],
                caller["agent_id"],
                kind,
                resource,
                pay_to,
                str(amount),
                now,
                now,
            ),
        )
        leaf = f"{caller['delegation_id']}#{action_id}"
        outcome = self._ledger.reserve(
            f"{leaf}:reserve", caller["delegation_id"], leaf, caller["agent_id"], amount
        )
        if outcome == "rejected":
            remaining = self._remaining(caller["delegation_id"])
            self._set(action_id, state="REFUSED", reason="insufficient_authority")
            raise Refused(
                "insufficient_authority",
                {
                    "action_id": action_id,
                    "resource": resource,
                    "requested_usd": str(amount),
                    "remaining_usd": str(remaining),
                },
            )
        return action_id

    def _close_leaf(
        self, action: sqlite3.Row | dict[str, Any], actual: Decimal | None, outcome: str
    ) -> None:
        leaf = f"{action['delegation_id']}#{action['action_id']}"
        if actual is not None and actual > 0:
            self._ledger.consume(f"{leaf}:consume", leaf, actual)
        self._ledger.settle(f"{leaf}:settle", leaf, outcome)

    # -- model call (provider key held here, like the gateway) ----------

    def model_call(
        self, token: str, messages: list[dict[str, str]], max_tokens: int
    ) -> dict[str, Any]:
        if self._provider is None:
            raise RuntimeError("no model provider configured")
        caller = self._caller(token)
        p = self._provider
        ceiling = (
            estimate_prompt_tokens(messages) * p.input_usd_per_token
            + max_tokens * p.output_usd_per_token
        )
        action_id = self._admit(caller, "model", p.name, ceiling, None)
        if not self._transition(action_id, "RESERVING", state="CALLING"):
            raise Refused("reservation_released", {"action_id": action_id})
        text, prompt_tokens, completion_tokens = p.complete(messages, max_tokens)
        actual = min(
            prompt_tokens * p.input_usd_per_token + completion_tokens * p.output_usd_per_token,
            ceiling,
        )
        with self._conn() as conn:
            action = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        self._close_leaf(action, actual, "completed")
        self._set(action_id, state="SETTLED", actual_usd=str(actual), delivered=1)
        return {"text": text, "cost_usd": str(actual), "action_id": action_id}

    # -- x402 purchase (payer key held here) ----------------------------

    def pay(self, token: str, method: str, url: str, **request: Any) -> dict[str, Any]:
        caller = self._caller(token)
        probe = self._http.request(method, url, **request)
        if probe.status_code != 402:
            return {"status": probe.status_code, "body": probe.json(), "paid": False}
        required = PaymentRequired.model_validate(
            json.loads(b64decode(probe.headers["payment-required"]))
        )
        options = [
            a
            for a in required.accepts
            if a.scheme == "exact" and a.network == NETWORK and a.asset.lower() == USDC.lower()
        ]
        if not options:
            raise Refused("unsupported_payment_option", {"resource": url})
        option = options[0]
        amount = _usd(option.amount)

        action_id = self._admit(caller, "x402", url, amount, option.pay_to)

        client = x402ClientSync()
        client.register(NETWORK, ExactEvmScheme(signer=self._payer))
        payload = client.create_payment_payload(required.model_copy(update={"accepts": [option]}))
        auth = payload.payload["authorization"]
        if auth["to"].lower() != option.pay_to.lower() or int(auth["value"]) != int(option.amount):
            raise AssertionError("signed authorization does not match the admitted purchase")
        header = encode_payment_signature_header(payload)
        if not self._transition(
            action_id,
            "RESERVING",
            state="AUTHORIZED",
            nonce=auth["nonce"].lower(),
            valid_before=int(auth["validBefore"]),
            payload_sha256=hashlib.sha256(header.encode()).hexdigest(),
        ):
            # A reconcile released this reservation first. The signature never
            # leaves this process, so nothing can settle against it.
            raise Refused("reservation_released", {"action_id": action_id, "resource": url})

        delivered, body, status = False, None, None
        try:
            response = self._http.request(
                method, url, headers={"PAYMENT-SIGNATURE": header}, **request
            )
            status = response.status_code
            delivered = status == 200
            body = response.json() if delivered else None
        except Exception as exc:  # the payment may still have settled; the chain decides
            body = {"transport_error": type(exc).__name__}
        self._set(action_id, delivered=int(delivered))
        state = self._reconcile_action(action_id)
        return {
            "status": status,
            "body": body,
            "paid": state == "SETTLED",
            "state": state,
            "action_id": action_id,
        }

    # -- reconciliation: the chain, not the seller, decides ---------------

    def _reconcile_action(self, action_id: str) -> str:
        with self._conn() as conn:
            action = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if action["state"] in TERMINAL:
            return str(action["state"])
        if action["state"] == "RESERVING":
            # Reserved but never signed or called. Claim it first, so a process
            # still working on it can no longer move it to AUTHORIZED or CALLING.
            if not self._transition(action_id, "RESERVING", state="ABANDONING"):
                return self._reconcile_action(action_id)
            action = dict(action, state="ABANDONING")
        if action["state"] == "ABANDONING":
            # A signature only ever leaves this process after AUTHORIZED is
            # durable, so nothing can settle against this reservation: release it.
            self._close_leaf(action, None, "abandoned")
            self._set(action_id, state="ABANDONED")
            return "ABANDONED"
        if action["state"] == "CALLING":
            # The model call may have completed and been billed before the crash,
            # and its real cost is unknown: count the reserved ceiling.
            self._close_leaf(action, Decimal(action["amount_usd"]), "completed")
            self._set(action_id, state="SETTLED", actual_usd=action["amount_usd"])
            return "SETTLED"
        payer = self._payer.address
        if self._chain.authorization_state(payer, action["nonce"]):
            event = self._chain.used_event(payer, action["nonce"])
            self._close_leaf(action, Decimal(action["amount_usd"]), "settled")
            self._set(
                action_id,
                state="SETTLED",
                actual_usd=action["amount_usd"],
                tx=getattr(event, "tx", None),
            )
            return "SETTLED"
        if self._clock() > action["valid_before"]:
            # EIP-3009: past validBefore this authorization can never settle.
            self._close_leaf(action, None, "expired")
            self._set(action_id, state="EXPIRED")
            return "EXPIRED"
        return "AUTHORIZED"  # still settleable: keep the reservation held

    def reconcile(self) -> dict[str, str]:
        with self._conn() as conn:
            open_ids = [
                r["action_id"]
                for r in conn.execute(
                    "SELECT action_id FROM actions "
                    "WHERE state NOT IN ('REFUSED','SETTLED','EXPIRED','ABANDONED')"
                )
            ]
        return {a: self._reconcile_action(a) for a in open_ids}

    # -- the economic record of one unit of work ------------------------

    def record(self, work_id: str) -> dict[str, Any]:
        root_id = f"work:{work_id}"

        def tree(delegation_id: str) -> dict[str, Any]:
            s = self._ledger.get(delegation_id)
            assert s is not None
            node = {
                "delegation_id": s.delegation_id,
                "agent_id": s.agent_id,
                "authority_usd": str(s.authority_usd),
                "consumed_usd": str(s.consumed_usd),
                "reserved_for_children_usd": str(s.child_reserved_usd),
                "remaining_usd": str(s.active_reservation_usd if s.active else Decimal("0")),
                "returned_to_parent_usd": None if s.active else str(s.released_usd),
                "active": s.active,
                "invariant": self._ledger.invariant(s.delegation_id).label,
            }
            kids = [c for c in self._ledger.children(delegation_id) if "#" not in c.delegation_id]
            if kids:
                node["delegations"] = [tree(c.delegation_id) for c in kids]
            return node

        with self._conn() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM actions WHERE work_id = ? ORDER BY created_at", (work_id,)
                )
            ]
        for r in rows:
            r.pop("payload_sha256", None)
            if r["kind"] == "x402" and r["state"] == "SETTLED":
                r["chain_authorization_used"] = self._chain.authorization_state(
                    self._payer.address, r["nonce"]
                )
        settled = [r for r in rows if r["state"] == "SETTLED"]
        return {
            "work_id": work_id,
            "authority": tree(root_id),
            "settled_spend_usd": str(
                sum((Decimal(r["actual_usd"]) for r in settled), Decimal("0"))
            ),
            "actions": [r for r in rows if r["state"] != "REFUSED"],
            "refused_before_authorization": [r for r in rows if r["state"] == "REFUSED"],
            "payer": self._payer.address,
        }
