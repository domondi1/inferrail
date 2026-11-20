"""Web Read on more than one x402 network (staged, off by default).

These prove that extra networks are refused unless explicitly listed (and, for mainnets,
separately approved), that each network is offered with its own receiving address in one 402,
that a Polygon (EVM) and a Solana (SVM) payment each deliver once and settle once against the
right ledger key, that a retry from the same wallet is answered without a second charge, and
that the existing Base offer is unchanged when nothing extra is configured.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("solders")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import decode_payment_required  # noqa: E402
from eth_account import Account  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from x402.client import x402ClientSync  # noqa: E402
from x402.http.utils import (  # noqa: E402
    decode_payment_signature_header,
    encode_payment_signature_header,
)
from x402.mechanisms.evm.exact import ExactEvmScheme  # noqa: E402
from x402.mechanisms.svm import KeypairSigner  # noqa: E402
from x402.mechanisms.svm.exact import ExactSvmScheme  # noqa: E402
from x402.schemas import (  # noqa: E402
    PaymentPayload,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)
from x402.schemas.responses import SettleResponse  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = sys.modules.get("web_read_service") or _load(
    "web_read_service", ROOT / "hosted" / "web_read" / "service.py"
)
fetch = sys.modules["web_read_fetch"]

BASE_PAY_TO = "0x" + "7" * 40
POLYGON_PAY_TO = "0x" + "8" * 40
SOL_PAY_TO = str(Keypair().pubkey())
FEE_PAYER = str(Keypair().pubkey())
BLOCKHASH = str(Hash.new_unique())
ARTICLE = (
    "<html><head><title>T</title></head><body>"
    "<main><h1>Hello</h1><p>World.</p></main></body></html>"
)
HTML = {"content-type": "text/html; charset=utf-8"}
SITE = {
    "news.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
    "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML),
}


def _site(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get(f"{request.url.host}{request.url.path}")
        return (
            httpx.Response(404)
            if r is None
            else httpx.Response(r.status_code, headers=r.headers, content=r.content)
        )

    return httpx.MockTransport(handler)


async def _public_dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@dataclass
class MultiFacilitator:
    """A fake facilitator for Base Sepolia, Polygon Amoy and Solana devnet. It accepts any
    payment the SDK built (signatures are checked by the service's own code paths where that
    matters) and settles each payment key once, like a real facilitator."""

    networks: tuple[str, ...] = (svc.TESTNET, svc.POLYGON_MAINNET, svc.SOLANA_DEVNET)
    settled: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get_supported(self) -> SupportedResponse:
        kinds = []
        for n in self.networks:
            extra = {"feePayer": FEE_PAYER} if n.startswith("solana:") else None
            kinds.append(SupportedKind(x402_version=2, scheme="exact", network=n, extra=extra))
        return SupportedResponse(kinds=kinds)

    @staticmethod
    def _key(payload: PaymentPayload) -> tuple[str, str]:
        ref = svc._payment_ref(payload)
        assert ref is not None, "the service must be able to key every payment it accepts"
        return ref

    async def verify(self, payload: PaymentPayload, requirements: Any) -> VerifyResponse:
        key, payer = self._key(payload)
        self.calls.append(("verify", key))
        if key in self.settled:
            return VerifyResponse(is_valid=False, invalid_reason="already_used", payer=payer)
        return VerifyResponse(is_valid=True, payer=payer)

    async def settle(self, payload: PaymentPayload, requirements: Any) -> SettleResponse:
        key, payer = self._key(payload)
        self.calls.append(("settle", key))
        if key in self.settled:
            return SettleResponse(
                success=False, error_reason="already_used", transaction="",
                network=requirements.network, payer=payer,
            )  # fmt: skip
        self.settled.add(key)
        return SettleResponse(
            success=True, transaction="0x" + hashlib.sha256(key.encode()).hexdigest(),
            network=requirements.network, payer=payer, amount=requirements.amount,
        )  # fmt: skip

    def settles(self) -> int:
        return sum(1 for c in self.calls if c[0] == "settle")


EXTRAS = f"{svc.POLYGON_MAINNET}={POLYGON_PAY_TO},{svc.SOLANA_DEVNET}={SOL_PAY_TO}"


def _settings(tmp_path: Path, extras: str | None = EXTRAS) -> Any:
    env = {
        "READ_PAY_TO": BASE_PAY_TO,
        "READ_RESOURCE_URL": "http://testserver/v1/page",
        "READ_DATA_DIR": str(tmp_path),
        "BG_OPERATOR_TOKEN": "op-token",
    }
    if extras:
        env["READ_X402_EXTRA_NETWORKS"] = extras
        # Polygon is mainnet-only in the SDK; the fake facilitator moves no money.
        env["READ_X402_EXTRA_MAINNET_APPROVED"] = "1"
    return svc.Settings.from_env(env)


def _client(tmp_path: Path, fac: MultiFacilitator, extras: str | None = EXTRAS) -> TestClient:
    settings = _settings(tmp_path, extras)
    reader = fetch.Fetcher(settings.user_agent, transport=_site(SITE), resolve=_public_dns)
    return TestClient(svc.create_app(settings, facilitator=fac, fetcher=reader))


class _Mint:
    from solders.pubkey import Pubkey as _Pk

    token_program = _Pk.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    decimals = 6


@pytest.fixture(autouse=True)
def _offline_mint(monkeypatch):
    """The SDK's Solana client reads mint metadata over RPC; tests are offline."""
    import x402.mechanisms.svm.exact.client as svm_client

    monkeypatch.setattr(svm_client, "get_cached_mint_metadata", lambda *a, **k: _Mint())


def _sign(c: TestClient, network: str, body: dict[str, Any], signer: Any = None) -> tuple[str, Any]:
    """A buyer's x402 client paying the offer for `network` (and only that one)."""
    req = decode_payment_required(c.post("/v1/page", json=body))
    accept = next(a for a in req.accepts if a.network == network)
    req = req.model_copy(update={"accepts": [accept]})
    buyer = x402ClientSync()
    if network.startswith("solana:"):
        signer = signer or KeypairSigner(Keypair())
        accept.extra = {**(accept.extra or {}), "recentBlockhash": BLOCKHASH}
        buyer.register(network, ExactSvmScheme(signer=signer, rpc_url="http://offline.invalid"))
    else:
        signer = signer or Account.create()
        buyer.register(network, ExactEvmScheme(signer=signer))
    payload = buyer.create_payment_payload(req)
    return encode_payment_signature_header(payload), signer


def _stats(c: TestClient) -> dict[str, Any]:
    return c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()


# -- configuration ----------------------------------------------------------------------------


def test_no_extra_networks_by_default_and_the_base_offer_is_unchanged(tmp_path):
    c = _client(tmp_path, MultiFacilitator(), extras=None)
    (accept,) = decode_payment_required(
        c.post("/v1/page", json={"url": "https://news.test/a"})
    ).accepts
    assert (accept.network, accept.pay_to, accept.amount) == (svc.TESTNET, BASE_PAY_TO, "2000")


@pytest.mark.parametrize(
    ("value", "approved", "message"),
    [
        ("eip155:42161=0x" + "1" * 40, False, "not supported"),
        ("eip155:80002=0x" + "1" * 40, False, "not supported"),
        (f"{svc.POLYGON_MAINNET}=0x" + "1" * 40, False, "separate approval"),
        (f"{svc.SOLANA_MAINNET}={SOL_PAY_TO}", False, "separate approval"),
        (f"{svc.SOLANA_DEVNET}=notbase58!", False, "invalid payTo"),
        (f"{svc.SOLANA_DEVNET}=0x" + "1" * 40, False, "invalid payTo"),
        (f"{svc.SOLANA_DEVNET}={SOL_PAY_TO},{svc.SOLANA_DEVNET}={SOL_PAY_TO}", False, "twice"),
        (f"{svc.POLYGON_MAINNET}=0xnothex", True, "invalid payTo"),
        (f"{svc.TESTNET}=0x" + "1" * 40, False, "not supported"),
    ],
)
def test_extra_networks_are_validated_and_mainnets_need_their_own_approval(
    tmp_path, value, approved, message
):
    env = {"READ_PAY_TO": BASE_PAY_TO, "READ_RESOURCE_URL": "https://x.test/v1/page",
           "READ_X402_EXTRA_NETWORKS": value}  # fmt: skip
    if approved:
        env["READ_X402_EXTRA_MAINNET_APPROVED"] = "1"
    with pytest.raises(RuntimeError, match=message):
        svc.Settings.from_env(env)


def test_approved_mainnet_extras_parse_with_their_own_addresses():
    env = {
        "READ_PAY_TO": BASE_PAY_TO, "READ_RESOURCE_URL": "https://x.test/v1/page",
        "READ_X402_EXTRA_NETWORKS": (
            f"{svc.POLYGON_MAINNET}={POLYGON_PAY_TO}, {svc.SOLANA_MAINNET}={SOL_PAY_TO}"
        ),
        "READ_X402_EXTRA_MAINNET_APPROVED": "1",
    }  # fmt: skip
    s = svc.Settings.from_env(env)
    assert s.accepted() == (
        (svc.TESTNET, BASE_PAY_TO),
        (svc.POLYGON_MAINNET, POLYGON_PAY_TO),
        (svc.SOLANA_MAINNET, SOL_PAY_TO),
    )


# -- the offer ----------------------------------------------------------------------------------


def test_one_402_offers_every_network_with_its_own_address_and_usdc(tmp_path):
    c = _client(tmp_path, MultiFacilitator())
    req = decode_payment_required(c.post("/v1/page", json={"url": "https://news.test/a"}))
    by_net = {a.network: a for a in req.accepts}
    assert set(by_net) == {svc.TESTNET, svc.POLYGON_MAINNET, svc.SOLANA_DEVNET}
    assert req.accepts[0].network == svc.TESTNET  # the primary stays first
    assert by_net[svc.TESTNET].pay_to == BASE_PAY_TO
    assert by_net[svc.POLYGON_MAINNET].pay_to == POLYGON_PAY_TO
    assert by_net[svc.SOLANA_DEVNET].pay_to == SOL_PAY_TO
    assert {a.amount for a in req.accepts} == {"2000"}  # 6-decimal USDC everywhere
    from x402.mechanisms.svm.utils import get_usdc_address

    assert by_net[svc.SOLANA_DEVNET].asset == get_usdc_address(svc.SOLANA_DEVNET)
    assert by_net[svc.SOLANA_DEVNET].extra["feePayer"] == FEE_PAYER


# -- paying on each network ---------------------------------------------------------------------


@pytest.mark.parametrize("network", [svc.TESTNET, svc.POLYGON_MAINNET, svc.SOLANA_DEVNET])
def test_a_paid_read_on_each_network_delivers_once_settles_once_and_is_keyed_right(
    tmp_path, network
):
    fac = MultiFacilitator()
    c = _client(tmp_path, fac)
    body = {"url": "https://news.test/a", "request_id": "r1"}
    header, signer = _sign(c, network, body)
    r = c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 200, r.text
    assert "# Hello" in r.json()["content_markdown"]
    assert fac.settles() == 1
    ref = r.json()["receipt"]["payment_ref"]
    if network.startswith("solana:"):
        assert ref.startswith(f"{signer.address}:svm:")  # base58 case kept
    else:
        assert ref.startswith(signer.address.lower() + ":0x")
    stats = _stats(c)
    assert stats["settled_reads"] == 1 and stats["revenue_usd"] == "0.002"


@pytest.mark.parametrize("network", [svc.POLYGON_MAINNET, svc.SOLANA_DEVNET])
def test_a_retry_with_a_new_signature_from_the_same_wallet_is_not_charged_again(tmp_path, network):
    fac = MultiFacilitator()
    c = _client(tmp_path, fac)
    body = {"url": "https://news.test/a", "request_id": "same"}
    first, signer = _sign(c, network, body)
    assert c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": first}).status_code == 200
    second, _ = _sign(c, network, body, signer=signer)  # a fresh payment, same wallet, same request
    assert second != first
    r = c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": second})
    assert r.status_code == 200 and r.headers.get("x-inferrail-recovered") == "1"
    assert fac.settles() == 1
    assert _stats(c)["settled_reads"] == 1


def test_the_same_signed_solana_payment_is_never_delivered_twice(tmp_path):
    fac = MultiFacilitator()
    c = _client(tmp_path, fac)
    header, _ = _sign(c, svc.SOLANA_DEVNET, {"url": "https://news.test/a"})
    assert c.post("/v1/page", json={"url": "https://news.test/a"},
                  headers={"PAYMENT-SIGNATURE": header}).status_code == 200  # fmt: skip
    again = c.post(
        "/v1/page", json={"url": "https://news.test/a"}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert again.status_code in (200, 402)
    assert fac.settles() <= 2 and len(fac.settled) == 1  # one settled key, whatever the path
    assert _stats(c)["settled_reads"] == 1


def test_a_solana_payment_is_refused_when_solana_is_not_offered(tmp_path):
    offered = _client(tmp_path / "a", MultiFacilitator())
    header, _ = _sign(offered, svc.SOLANA_DEVNET, {"url": "https://news.test/a"})
    fac = MultiFacilitator()
    base_only = _client(tmp_path / "b", fac, extras=None)
    r = base_only.post(
        "/v1/page", json={"url": "https://news.test/a"}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert r.status_code == 402
    assert fac.settles() == 0 and _stats(base_only)["settled_reads"] == 0


# -- local checks used for free retries ---------------------------------------------------------


def _svm_payload(tmp_path: Path, amount_override: int | None = None, pay_to: str | None = None):
    c = _client(tmp_path, MultiFacilitator())
    header, signer = _sign(c, svc.SOLANA_DEVNET, {"url": "https://news.test/a"})
    payload = decode_payment_signature_header(header)
    return _settings(tmp_path), payload, signer


def test_svm_signed_by_payer_accepts_ours_and_rejects_a_tampered_signature(tmp_path):
    settings, payload, signer = _svm_payload(tmp_path)
    assert svc._svm_signed_by_payer(payload, settings) == signer.address
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(base64.b64decode(payload.payload["transaction"]))
    sigs = list(tx.signatures)
    payer_index = [str(k) for k in tx.message.account_keys].index(signer.address)
    sigs[payer_index] = Keypair().sign_message(b"not this message")
    tx.signatures = sigs
    forged = payload.model_copy(
        update={"payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
    )
    assert svc._svm_signed_by_payer(forged, settings) is None


def test_svm_signed_by_payer_rejects_a_payment_to_another_address(tmp_path):
    settings, payload, _ = _svm_payload(tmp_path)
    other = svc.Settings.from_env({
        "READ_PAY_TO": BASE_PAY_TO, "READ_RESOURCE_URL": "http://testserver/v1/page",
        "READ_X402_EXTRA_NETWORKS": f"{svc.SOLANA_DEVNET}={Keypair().pubkey()}",
    })  # fmt: skip
    assert svc._svm_signed_by_payer(payload, other) is None
    assert svc._svm_signed_by_payer(payload, settings) is not None
