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

import hashlib
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MAINNET, TESTNET = 4217, 42431


@dataclass(frozen=True)
class MppSettings:
    chain_id: int
    secret_key: str
    realm: str

    @classmethod
    def from_env(cls, env: dict[str, str], realm: str) -> MppSettings | None:
        raw = env.get("READ_MPP_CHAIN_ID", "").strip()
        if not raw:
            return None
        chain_id = int(raw)
        if chain_id not in (MAINNET, TESTNET):
            raise RuntimeError(f"READ_MPP_CHAIN_ID must be {MAINNET} or {TESTNET}")
        if chain_id == MAINNET and env.get("READ_MPP_MAINNET_APPROVED") != "1":
            raise RuntimeError("Tempo mainnet sales require READ_MPP_MAINNET_APPROVED=1")
        return cls(chain_id=chain_id, secret_key=_secret(env), realm=realm)


def _secret(env: dict[str, str]) -> str:
    """HMAC key for challenge ids. Stable across restarts when derived from a secret the
    service already has, so a challenge issued before a deploy still validates after it."""
    if env.get("MPP_SECRET_KEY"):
        return env["MPP_SECRET_KEY"]
    seed = env.get("CDP_API_KEY_SECRET") or env.get("BG_OPERATOR_TOKEN")
    if seed:
        return hashlib.sha256(f"inferrail-web-read-mpp|{seed}".encode()).hexdigest()
    return secrets.token_hex(32)  # challenges then expire with the process


class TempoRail:
    def __init__(self, cfg: MppSettings, pay_to: str, price_usd: str) -> None:
        from mpp.methods.tempo import server as tempo
        from mpp.server import Mpp

        # The SDK's per-process replay store is enough here: a transaction credential is a
        # signed transfer with an expiring nonce (the chain rejects a second broadcast), and
        # the ledger keys every MPP sale by its transaction hash.
        self.cfg = cfg
        self.price = price_usd
        self.server = Mpp(
            methods=tempo.charge(recipient=pay_to, chain_id=cfg.chain_id),
            realm=cfg.realm,
            secret_key=cfg.secret_key,
        )
        self._charge = self.server.charge(amount=price_usd)
        self.atomic = int(Decimal(price_usd) * 10**6)

    async def challenge_headers(self, url: str) -> list[tuple[str, str]]:
        """The WWW-Authenticate header(s) of a fresh charge challenge for this route."""
        from mpp.server import Request as MppRequest

        result = await self._charge(MppRequest(method="POST", url=url, headers={}))
        challenge = getattr(result, "challenge", None)
        if challenge is None:
            return []
        return [
            (k, v) for k, v in challenge.headers.multi_items() if k.lower() == "www-authenticate"
        ]

    async def validate(self, authorization: str) -> tuple[Any, str, str] | None:
        """(credential, payer, credential type) when the credential is a valid, unspent payment
        for this route's price; None otherwise. Nothing is broadcast."""
        try:
            v = await self.server.validate_credential(
                authorization, intent="charge", request={"amount": self.price}
            )
        except Exception:
            return None
        payload = v.credential.payload
        kind = payload.get("type") if isinstance(payload, dict) else getattr(payload, "type", "")
        return v.credential, (v.payer or "").lower(), str(kind)

    async def settle(self, credential: Any) -> Any:
        """Broadcast the transfer (or confirm an already-broadcast one). Returns the receipt."""
        return await self.server.broadcast_credential(
            credential, intent="charge", request={"amount": self.price}
        )


def is_mpp_authorization(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith("payment ")
