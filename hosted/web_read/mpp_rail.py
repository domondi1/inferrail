"""MPP (Machine Payments Protocol) on Tempo for Web Read, next to x402 on the same route.

Same promise as the x402 path: **the buyer pays only for a delivered page.** A Tempo charge
credential is a signed transfer the client hands us; it is validated first (no broadcast), the
page is fetched, and only then is the transfer broadcast. A refusal never broadcasts it.

The rare `hash` credential (the client already broadcast the transfer itself) can't be held
back. It is accepted, and if delivery then fails the refusal is recorded as a refund owed
(`refund_owed` in the funnel) and reported to the client.

Off unless `READ_MPP_CHAIN_ID` is set: 42431 (Tempo testnet "Moderato") or 4217 (Tempo
mainnet, which also needs `READ_MPP_MAINNET_APPROVED=1`). The receiving address is Web Read's
own `READ_PAY_TO`; it's an EVM address, valid on Tempo too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAINNET, TESTNET = 4217, 42431


@dataclass(frozen=True)
class MppSettings:
    chain_id: int
    secret_key: str
    realm: str

    @classmethod
    def from_env(cls, env: dict[str, str], realm: str, prefix: str = "READ_") -> MppSettings | None:
        raw = env.get(f"{prefix}MPP_CHAIN_ID", "").strip()
        if not raw:
            return None
        chain_id = int(raw)
        if chain_id not in (MAINNET, TESTNET):
            raise RuntimeError(f"{prefix}MPP_CHAIN_ID must be {MAINNET} or {TESTNET}")
        if chain_id == MAINNET and env.get(f"{prefix}MPP_MAINNET_APPROVED") != "1":
            raise RuntimeError(f"Tempo mainnet sales require {prefix}MPP_MAINNET_APPROVED=1")
        return cls(chain_id=chain_id, secret_key=_secret(env, prefix), realm=realm)


def _secret(env: dict[str, str], prefix: str = "READ_") -> str:
    """HMAC key for challenge ids. Stable across restarts when derived from a secret the
    service already has, so a challenge issued before a deploy still validates after it.
    Each service gets its own key (Web Read's is unchanged), so one service's challenges never
    authenticate on another's route even before the route check."""
    own = "MPP_SECRET_KEY" if prefix == "READ_" else f"{prefix}MPP_SECRET_KEY"
    if env.get(own):
        return env[own]
    label = "inferrail-web-read-mpp" if prefix == "READ_" else f"inferrail-{prefix.lower()}mpp"
    seed = env.get("CDP_API_KEY_SECRET") or env.get("BG_OPERATOR_TOKEN")
    if seed:
        return hashlib.sha256(f"{label}|{seed}".encode()).hexdigest()
    return secrets.token_hex(32)  # challenges then expire with the process


class ReplayStore:
    """The SDK's replay store (`mpp.store.ExpiringStore`) in a SQLite file on the service's
    persistent disk. Its default store lives in process memory: a restart forgets which
    transaction hashes were already used, and at 10,000 entries it refuses new payments.

    A claim (`put_if_absent`) never expires; `put_if_absent_until` records expire at their
    deadline. Single process, like the ledger; stdlib `sqlite3`, no extra dependency."""

    NEVER = float("inf")

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL);
            """
        )

    def _prune(self) -> None:
        self._db.execute("DELETE FROM kv WHERE expires_at <= ?", (time.time(),))

    async def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE key=? AND expires_at > ?", (key, time.time())
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value), self.NEVER)
            )

    async def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))

    async def put_if_absent(self, key: str, value: Any) -> bool:
        return self._insert(key, value, self.NEVER)

    async def put_if_absent_until(self, key: str, value: Any, expires_at: float) -> bool:
        if not math.isfinite(expires_at) or expires_at <= time.time():
            raise ValueError("expires_at must be a finite future Unix timestamp")
        return self._insert(key, value, expires_at)

    def _insert(self, key: str, value: Any, expires_at: float) -> bool:
        with self._lock:
            self._prune()
            cur = self._db.execute(
                "INSERT OR IGNORE INTO kv VALUES (?,?,?)", (key, json.dumps(value), expires_at)
            )
        return cur.rowcount == 1


class TempoRail:
    def __init__(
        self,
        cfg: MppSettings,
        pay_to: str,
        price_usd: str,
        store: ReplayStore | None = None,
        description: str | None = None,
    ) -> None:
        from mpp.methods.tempo import server as tempo
        from mpp.server import Mpp

        # Replay protection: a transaction credential is a signed transfer with an expiring
        # nonce (the chain rejects a second broadcast); a hash credential is a transfer the
        # client already sent, so only the store stops it from buying a second page. The
        # ledger also keys every MPP sale by its transaction hash.
        self.cfg = cfg
        self.price = price_usd
        self.server = Mpp(
            methods=tempo.charge(recipient=pay_to, chain_id=cfg.chain_id),
            realm=cfg.realm,
            secret_key=cfg.secret_key,
            store=store,
        )
        # The challenge's `description` is what MPP indexes and clients show for the offer; it
        # isn't part of the challenge id, so changing it doesn't invalidate open challenges.
        self._charge = self.server.charge(
            amount=price_usd, **({"description": description} if description else {})
        )
        self.atomic = int(Decimal(price_usd) * 10**6)

    async def challenge_headers(self, url: str, method: str = "POST") -> list[tuple[str, str]]:
        """The WWW-Authenticate header(s) of a fresh charge challenge for this route."""
        from mpp.server import Request as MppRequest

        result = await self._charge(MppRequest(method=method, url=url, headers={}))
        challenge = getattr(result, "challenge", None)
        if challenge is None:
            return []
        return [
            (k, v) for k, v in challenge.headers.multi_items() if k.lower() == "www-authenticate"
        ]

    async def validate(self, authorization: str, url: str) -> tuple[Any, str, str] | str:
        """(credential, payer, credential type) when the credential is a valid, unspent payment
        for this route's price; otherwise the reason, as the error's class name (never its
        message, which can quote the credential). Nothing is broadcast.

        `url` is the paid request's URL. The SDK binds a challenge to our secret, realm, method,
        intent, amount and expiry but ignores its `_mppx_scope`, so a challenge issued for one
        route would also validate on any other route with the same secret and price. The scope
        is part of the challenge's HMAC, so checking it binds the payment to the route that
        issued it. Method and query aren't compared: one resource is sold over GET and POST."""
        if not _issued_for(authorization, urlsplit(url).path):
            return "RouteMismatch"
        try:
            v = await self.server.validate_credential(
                authorization, intent="charge", request={"amount": self.price}
            )
        except Exception as exc:
            return type(exc).__name__
        payload = v.credential.payload
        kind = payload.get("type") if isinstance(payload, dict) else getattr(payload, "type", "")
        return v.credential, (v.payer or "").lower(), str(kind)

    async def settle(self, credential: Any) -> Any:
        """Broadcast the transfer (or confirm an already-broadcast one). Returns the receipt."""
        return await self.server.broadcast_credential(
            credential, intent="charge", request={"amount": self.price}
        )


def _issued_for(authorization: str, path: str) -> bool:
    """Whether the credential echoes a challenge scoped to `path`. Unparseable or unscoped
    credentials fail closed; the HMAC check that follows rejects any altered scope."""
    from mpp import Credential

    try:
        echo = Credential.from_authorization(authorization).challenge
        padded = echo.request + "=" * (-len(echo.request) % 4)
        request = json.loads(base64.urlsafe_b64decode(padded))
        scope = request.get("_mppx_scope") if isinstance(request, dict) else None
        return isinstance(scope, dict) and scope.get("resource") == path
    except Exception:
        return False


def is_mpp_authorization(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith("payment ")
