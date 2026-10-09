"""Standalone Job-Safe Search seller; no gateway dependency."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements, ResourceInfo

from .contract import (
    DESCRIPTION,
    INPUT_EXAMPLE,
    OUTPUT_EXAMPLE,
    OUTPUT_SCHEMA,
    SearchRequest,
    atomic,
    usd,
)
from .economics import financial_state
from .payments import ChainEvidence, decode, encode, identity, recovery_requirements
from .store import Refused, Store
from .supplier import ExaSearch, FixtureSearch, MojeekBusinessSearch, SerpexSearch, SupplierFailure

SERVICE_NAME = "Inferrail Job-Safe Web Search"
SERVICE_TAGS = ["search", "web", "job-budget", "idempotency", "agent-payments"]


@dataclass(frozen=True)
class Config:
    pay_to: str
    resource_url: str
    token_secret: bytes
    network: str = "eip155:84532"
    price: int = 10000
    fee_bound: int = 1000
    minimum_margin: int = 5000
    realized_payment_fee: int | None = None
    risk_ceiling: int = 15_000_000
    supplier_prepaid_capital: int = 0
    recovery_from_block: str | None = None
    cache_ttl: int = 300
    job_ttl: int = 86400
    mainnet_approved: bool = False
    supplier_rights_confirmed: bool = False

    def validate(self, supplier: Any) -> None:
        if self.network not in ("eip155:84532", "eip155:8453"):
            raise ValueError("unsupported_network")
        if (
            type(self.cache_ttl) is not int
            or self.cache_ttl < 0
            or type(self.job_ttl) is not int
            or self.job_ttl <= 0
        ):
            raise ValueError("invalid_cache_or_job_lifetime")
        if len(self.token_secret) < 32:
            raise ValueError("token_secret_must_be_at_least_32_bytes")
        if len(self.pay_to) != 42 or not self.pay_to.startswith("0x") or int(self.pay_to, 16) == 0:
            raise ValueError("invalid_pay_to")
        if self.price < supplier.max_cost + self.fee_bound + self.minimum_margin:
            raise ValueError("price_below_cost_envelope")
        if (
            any(
                type(value) is not int or value < 0
                for value in (self.price, self.fee_bound, self.minimum_margin)
            )
            or self.price == 0
        ):
            raise ValueError("invalid_cost_envelope")
        if self.realized_payment_fee is not None and (
            type(self.realized_payment_fee) is not int
            or not 0 <= self.realized_payment_fee <= self.fee_bound
        ):
            raise ValueError("invalid_realized_payment_fee")
        # A deployment-wide fee cannot establish the actual charge for a payment:
        # account free tiers and invoice allocations can change between requests.
        if self.network == "eip155:8453" and self.realized_payment_fee is not None:
            raise ValueError("mainnet_payment_fee_requires_per_purchase_reconciliation")
        if (
            self.supplier_prepaid_capital < 0
            or not 0 < self.risk_ceiling
            or self.risk_ceiling + self.supplier_prepaid_capital > 20_000_000
        ):
            raise ValueError("risk_ceiling_out_of_range")
        if self.network == "eip155:8453":
            if not self.mainnet_approved or not self.supplier_rights_confirmed:
                raise ValueError("mainnet_requires_explicit_approval_and_supplier_rights")
            if supplier.name == "fixture" or not self.resource_url.startswith("https://"):
                raise ValueError("mainnet_requires_real_supplier_and_https")

    def requirements(self) -> PaymentRequirements:
        asset = (
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            if self.network == "eip155:8453"
            else "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        )
        return PaymentRequirements(
            scheme="exact",
            network=self.network,
            asset=asset,
            amount=str(self.price),
            pay_to=self.pay_to,
            max_timeout_seconds=3600,
            extra={
                "name": "USD Coin" if self.network == "eip155:8453" else "USDC",
                "version": "2",
                "paymentFlow": "upfront",
            },
        )


class Tokens:
    def __init__(self, secret: bytes):
        self.secret = secret

    def issue(self, job: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "job": job["id"],
                    "payer": job["payer"],
                    "budget": job["budget"],
                    "expires": job["expires"],
                },
                sort_keys=True,
            ).encode()
        ).decode()
        return body + "." + hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()

    def read(self, token: str, store: Store) -> dict[str, Any]:
        try:
            body, signature = token.split(".")
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(body))
            job = store.job(claims["job"])
            if not job or any(
                claims[k] != job[v]
                for k, v in [
                    ("job", "id"),
                    ("payer", "payer"),
                    ("budget", "budget"),
                    ("expires", "expires"),
                ]
            ):
                raise ValueError()
            if claims["expires"] <= time.time():
                raise ValueError()
            return job
        except (ValueError, KeyError, TypeError) as exc:
            raise Refused("invalid_or_expired_job_token") from exc


class SearchService:
    def __init__(self, config: Config, store: Store, facilitator: Any, chain: Any, supplier: Any):
        config.validate(supplier)
        self.config, self.store, self.facilitator, self.chain, self.supplier = (
            config,
            store,
            facilitator,
            chain,
            supplier,
        )
        self.tokens = Tokens(config.token_secret)
        self.requirements = config.requirements()
        store.bind_deployment(self.requirements, config.token_secret)
        self.description = DESCRIPTION.replace(
            "free 5-minute same-job cache", f"free {config.cache_ttl}-second same-job cache"
        )
        self.input_example = copy.deepcopy(INPUT_EXAMPLE)
        self.input_example["job_budget_usd"] = usd(config.price * 3)
        self.output_example = copy.deepcopy(OUTPUT_EXAMPLE)
        self.output_example["receipt"].update(
            {
                "charged_usd": usd(config.price),
                "provider": supplier.name,
                "financial_state": "RESOLVED"
                if config.realized_payment_fee is not None
                else "UNRESOLVED",
                "remaining_job_budget_usd": usd(config.price * 2),
            }
        )
        self.extensions = declare_discovery_extension(
            input=self.input_example,
            input_schema=SearchRequest.model_json_schema(),
            body_type="json",
            output=OutputConfig(example=self.output_example, schema=OUTPUT_SCHEMA),
        )
        self.extensions["bazaar"]["info"]["input"]["method"] = "POST"

    def challenge(self, error: str | None = None) -> JSONResponse:
        required = PaymentRequired(
            x402_version=2,
            resource=ResourceInfo(
                url=self.config.resource_url,
                description=self.description,
                mime_type="application/json",
                service_name=SERVICE_NAME,
                tags=SERVICE_TAGS,
            ),
            accepts=[self.requirements],
            extensions=self.extensions,
        )
        body = required.model_dump(by_alias=True, exclude_none=True)
        if error:
            body["error"] = error
        return JSONResponse(
            status_code=402, content=body, headers={"PAYMENT-REQUIRED": encode(body)}
        )

    def response(
        self, row: dict[str, Any], *, free: bool = False, cache: bool = False
    ) -> JSONResponse:
        job = self.store.job(row["job"])
        assert job is not None
        complete = row["state"] == "DELIVERED"
        extra_liability = self.store.extra_liability(row["id"])
        financial = financial_state({**row, "extra_payment_liability": extra_liability})
        paid = financial["settled_revenue"] not in (None, "0")
        if row["state"] == "PAYMENT_REJECTED":
            response = self.challenge(
                "Payment was rejected; retry with a new request_id and fresh signature."
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        content: dict[str, Any] = {
            "job_id": job["id"],
            "job_token": self.tokens.issue(job),
            "receipt": {
                "receipt_id": f"search-{row['id']}",
                "job_id": job["id"],
                "request_id": row["request_id"],
                "charged_usd": "0" if free else usd(row["price"]) if paid else None,
                "original_charge_usd": usd(row["price"]) if paid else None,
                "provider": row["provider"],
                "cache_hit": cache,
                "replayed": free,
                "failover": False,
                "remaining_job_budget_usd": usd(job["budget"] - job["committed"])
                if job["budget"] is not None
                else None,
                "economic_state": "SETTLED" if complete else row["state"],
                "transaction": row["tx"],
                "financial_state": "RESOLVED" if financial["resolved"] else "UNRESOLVED",
                "additional_payment_liability_usd": usd(extra_liability),
            },
        }
        if complete:
            content["results"] = json.loads(row["result"])
            content["receipt"]["result_count"] = len(content["results"])
        else:
            content["error"] = (
                "reconciliation_required"
                if row["state"]
                in ("SUPPLIER_UNKNOWN", "SERVICE_FAILED", "PAYMENT_UNKNOWN", "RESOLVED")
                else "in_progress"
            )
            content["retry"] = "Reuse request_id and original signature or job_token."
            content["payment_instruction"] = "Never sign a new payment for this request."
        headers = {"Cache-Control": "no-store"}
        if row["settlement"]:
            headers["PAYMENT-RESPONSE"] = encode(json.loads(row["settlement"]))
        if not complete:
            headers["Retry-After"] = "10"
        status = 402 if row["state"] == "PAYMENT_REJECTED" else 200 if complete else 202
        return JSONResponse(status_code=status, content=content, headers=headers)

    async def reconcile_observation(self, observed: dict[str, Any]) -> None:
        if not self.config.recovery_from_block:
            return
        payload = PaymentPayload.model_validate_json(observed["payload"])
        tx = await self.chain.find_transaction(payload, self.config.recovery_from_block)
        if tx and observed["purchase"] is not None:
            self.store.record_extra_payment(
                observed["purchase"],
                observed["payer"],
                observed["nonce"],
                tx,
                observed["amount"],
                pending_payload=observed["payload"],
            )
        if tx and await self.chain.confirmed(payload, tx):
            if observed["purchase"] is not None:
                self.store.record_extra_payment(
                    observed["purchase"],
                    observed["payer"],
                    observed["nonce"],
                    tx,
                    observed["amount"],
                )
            self.store.resolve_observation(
                observed["payer"],
                observed["nonce"],
                "SETTLED",
                tx,
                self.config.realized_payment_fee,
            )
        elif not tx:
            proof = getattr(self.chain, "settlement_impossible", None)
            if callable(proof) and await proof(payload):
                self.store.resolve_observation(observed["payer"], observed["nonce"], "NOT_SETTLED")

    async def observe_payment(
        self, signature: str | None, purchase: int | None = None
    ) -> dict[str, Any] | None:
        if not signature:
            return None
        try:
            payload = decode(signature)
            payer, nonce = identity(payload, recovery_requirements(payload, self.requirements))
        except Exception:
            return None
        observed = self.store.observe_payment(
            payload.model_dump_json(by_alias=True), payer, nonce, purchase
        )
        if observed is None:
            return None
        try:
            if observed["state"] == "OBSERVED":
                await self.reconcile_observation(observed)
        except Exception:
            # Persisted before RPC: restart reconciliation cannot lose a proxy-settled payment.
            pass
        return next(
            item
            for item in self.store.observed_payments(pending=False)
            if item["payer"] == payer and item["nonce"] == nonce
        )

    async def observe_extra_payment(self, row: dict[str, Any], signature: str | None) -> None:
        if not signature:
            return
        try:
            payload = decode(signature)
            payer, nonce = identity(payload, recovery_requirements(payload, self.requirements))
        except Exception:
            return
        # A forwarding buyer may settle a fresh authorization before this request
        # reaches us. The bearer job token can be used by another wallet, so the
        # payer must not be used to discard a possible incoming transfer.
        if payer != row["payer"] or nonce != row["nonce"]:
            await self.observe_payment(signature, row["id"])

    def token_context(self, request: SearchRequest) -> dict[str, Any] | None:
        if not request.job_token:
            return None
        job = self.tokens.read(request.job_token, self.store)
        if request.job_id is not None and request.job_id != job["id"]:
            raise Refused("job_token_mismatch")
        if request.job_budget_usd is not None and atomic(request.job_budget_usd) != job["budget"]:
            raise Refused("immutable_job_budget")
        return job

    async def handle(self, request: SearchRequest, signature: str | None) -> JSONResponse:
        job = self.token_context(request)
        if job:
            row = self.store.lookup(job["payer"], job["id"], request.request_id)
            if row:
                if row["fingerprint"] != request.fingerprint():
                    raise Refused("request_id_conflict")
                # A fresh signed payment is never settled for an existing request.
                await self.observe_extra_payment(row, signature)
                await self.advance(row["id"])
                return self.response(self.store.get(row["id"]), free=True)
            cached = self.store.cached(
                job["payer"],
                job["id"],
                request.request_id,
                request.fingerprint(),
                self.config.cache_ttl,
            )
            if cached:
                await self.observe_extra_payment(cached, signature)
                return self.response(cached, free=True, cache=True)
            if job["budget"] is not None and job["budget"] - job["committed"] < self.config.price:
                raise Refused("job_budget_exhausted")
        if not signature:
            if (
                request.job_budget_usd is not None
                and atomic(request.job_budget_usd) < self.config.price
            ):
                raise Refused("job_budget_exhausted")
            return self.challenge()
        try:
            payload = decode(signature)
            payer, nonce = identity(payload, recovery_requirements(payload, self.requirements))
        except Exception as exc:
            raise Refused("invalid_payment") from exc
        if job and job["payer"] != payer:
            raise Refused("payer_job_mismatch")
        job_id = (
            job["id"]
            if job
            else str(
                uuid.uuid5(uuid.NAMESPACE_URL, payer + ":" + (request.job_id or request.request_id))
            )
        )
        existing = self.store.lookup(payer, job_id, request.request_id)
        if payload.accepted != self.requirements:
            archived = (
                PaymentPayload.model_validate_json(existing["payload"]).accepted
                if existing is not None
                else None
            )
            if payload.accepted != archived:
                raise Refused("invalid_payment")
        if (
            existing is None
            and request.job_budget_usd is not None
            and atomic(request.job_budget_usd) < self.config.price
        ):
            raise Refused("job_budget_exhausted")
        # Do not trust buyer-supplied discovery/resource metadata during indexing.
        payload = payload.model_copy(
            update={
                "extensions": self.extensions,
                "resource": ResourceInfo(
                    url=self.config.resource_url,
                    description=self.description,
                    mime_type="application/json",
                ),
            }
        )
        if existing is None and self.store.payment_fee_blocked(self.config.fee_bound):
            raise Refused("payment_fee_bound_breached")
        if existing is None and self.store.supplier_blocked(self.supplier.name):
            raise Refused("supplier_cost_contract_breached")
        row, new = self.store.reserve(
            payer=payer,
            job=job_id,
            request=request.request_id,
            fingerprint=request.fingerprint(),
            budget=atomic(request.job_budget_usd) if request.job_budget_usd is not None else None,
            authenticated=job is not None,
            price=self.config.price,
            supplier_bound=self.supplier.max_cost,
            fee_bound=self.config.fee_bound,
            nonce=nonce,
            payload=payload.model_dump_json(by_alias=True),
            body=request.model_dump_json(exclude={"job_token"}),
            risk_ceiling=self.config.risk_ceiling,
            expires=time.time() + self.config.job_ttl,
            provider=self.supplier.name,
            cache_ttl=self.config.cache_ttl,
        )
        if not new:
            await self.observe_extra_payment(row, signature)
        await self.advance(row["id"])
        return self.response(self.store.get(row["id"]), free=not new or existing is not None)

    async def advance(self, purchase: int) -> None:
        row = self.store.get(purchase)
        payload = PaymentPayload.model_validate_json(row["payload"])
        # A stale offer may recover its result or chain evidence, but cannot initiate
        # a new settlement under the current offer.
        if row["state"] == "RESERVED" and payload.accepted != self.requirements:
            return
        if row["state"] == "RESERVED" and self.store.payment_fee_blocked(self.config.fee_bound):
            return
        if row["state"] == "RESERVED" and self.store.supplier_blocked(self.supplier.name):
            return
        if row["state"] == "RESERVED" and self.store.transition(purchase, "RESERVED", "VERIFYING"):
            verified = None
            try:
                verified = await self.facilitator.verify(payload, self.requirements)
            except Exception:
                if not self.config.recovery_from_block:
                    self.store.transition(purchase, "VERIFYING", "RESERVED")
                    return
            if (verified is None or not verified.is_valid) and self.config.recovery_from_block:
                # Some proxies settle before forwarding the signed authorization.
                # A spent nonce alone is insufficient: require its exact finalized USDC transfer.
                try:
                    tx = await self.chain.find_transaction(payload, self.config.recovery_from_block)
                except Exception:
                    self.store.transition(purchase, "VERIFYING", "RESERVED")
                    return
                if tx:
                    self.store.transition(
                        purchase,
                        "VERIFYING",
                        "FINALITY_PENDING",
                        tx=tx,
                        settlement=json.dumps(
                            {
                                "success": True,
                                "transaction": tx,
                                "network": self.config.network,
                                "payer": row["payer"],
                                "amount": str(row["price"]),
                            }
                        ),
                        liability=row["price"],
                        variable_fees=0,
                    )
                    await self.advance(purchase)
                    return
            if verified is not None and not verified.is_valid and self.config.recovery_from_block:
                self.store.transition(
                    purchase, "VERIFYING", "PAYMENT_UNKNOWN", liability=row["price"]
                )
                return
            if verified is None:
                self.store.transition(purchase, "VERIFYING", "RESERVED")
                return
            if not verified.is_valid or (verified.payer or "").lower() != row["payer"]:
                self.store.transition(
                    purchase, "VERIFYING", "PAYMENT_REJECTED", supplier_cogs=0, variable_fees=0
                )
                return
            if not self.store.transition(purchase, "VERIFYING", "SETTLING"):
                return
            try:
                settled = await self.facilitator.settle(payload, self.requirements)
            except Exception:
                self.store.transition(
                    purchase, "SETTLING", "PAYMENT_UNKNOWN", liability=row["price"]
                )
                return
            data = settled.model_dump(by_alias=True, exclude_none=True)
            # Failure is conservatively UNKNOWN: a transfer may have broadcast.
            if (
                not settled.success
                or not settled.transaction
                or str(settled.network) != self.config.network
            ):
                self.store.transition(
                    purchase,
                    "SETTLING",
                    "PAYMENT_UNKNOWN",
                    tx=settled.transaction or None,
                    settlement=json.dumps(data),
                    liability=row["price"],
                )
                return
            self.store.transition(
                purchase,
                "SETTLING",
                "FINALITY_PENDING",
                tx=settled.transaction,
                settlement=json.dumps(data),
                liability=row["price"],
                variable_fees=self.config.realized_payment_fee,
            )
        row = self.store.get(purchase)
        if row["state"] != "FINALITY_PENDING":
            return
        try:
            confirmed = await self.chain.confirmed(payload, row["tx"])
        except Exception:
            return
        if not confirmed:
            return
        # Recheck the stored economic envelope against the current deployment before dispatch.
        if (
            self.store.supplier_blocked(self.supplier.name)
            or self.store.payment_fee_blocked(self.config.fee_bound)
            or self.config.fee_bound > row["fee_bound"]
            or row["provider"] != self.supplier.name
            or self.supplier.max_cost > row["supplier_bound"]
            or row["price"] < self.supplier.max_cost + row["fee_bound"] + self.config.minimum_margin
            or int(self.requirements.amount) != row["price"]
        ):
            return
        # Commit intent BEFORE external supplier call, so crashes cannot buy twice.
        if not self.store.transition(purchase, "FINALITY_PENDING", "SUPPLIER_INFLIGHT"):
            return
        try:
            result = await self.supplier.search(SearchRequest.model_validate_json(row["request"]))
            if result.cogs is not None and (type(result.cogs) is not int or result.cogs < 0):
                raise ValueError("invalid_supplier_billing")
            # Preserve known supplier billing even if the delivered output is unusable.
            self.store.transition(
                purchase,
                "SUPPLIER_INFLIGHT",
                "SUPPLIER_INFLIGHT",
                supplier_cogs=result.cogs,
                supplier_reference=result.provider_request_id,
            )
            if result.cogs is not None and result.cogs > row["supplier_bound"]:
                raise ValueError("cost_outside_envelope")
            if not result.hits:
                raise ValueError("empty_supplier_result")
            from .contract import SearchHit

            hits = [SearchHit.model_validate(hit).model_dump() for hit in result.hits]
        except Exception as exc:
            if isinstance(exc, SupplierFailure):
                self.store.transition(
                    purchase,
                    "SUPPLIER_INFLIGHT",
                    "SUPPLIER_INFLIGHT",
                    supplier_cogs=exc.cogs,
                    supplier_reference=exc.provider_request_id,
                )
            with self.store.transaction() as conn:
                # Class only: upstream exception text may contain credentials or response data.
                self.store.event(
                    conn, purchase, "SUPPLIER_FAILURE", failure_type=type(exc).__name__
                )
            current = self.store.get(purchase)
            state = "SERVICE_FAILED" if current["supplier_cogs"] is not None else "SUPPLIER_UNKNOWN"
            self.store.transition(purchase, "SUPPLIER_INFLIGHT", state)
            return
        self.store.transition(
            purchase,
            "SUPPLIER_INFLIGHT",
            "DELIVERED",
            result=json.dumps(hits),
            supplier_cogs=result.cogs,
            supplier_reference=result.provider_request_id,
            liability=0,
            delivered=time.time(),
        )

    def prepare_recovery(self) -> None:
        # Normalize all interrupted intents before any network call can fail.
        for row in self.store.outstanding():
            if row["state"] == "VERIFYING":
                self.store.transition(row["id"], "VERIFYING", "RESERVED")
            elif row["state"] == "SUPPLIER_INFLIGHT":
                self.store.transition(row["id"], "SUPPLIER_INFLIGHT", "SUPPLIER_UNKNOWN")

    async def recover(self, from_block: str, *, startup: bool = True) -> None:
        if startup:
            self.prepare_recovery()
        for observed in self.store.observed_payments():
            try:
                await self.reconcile_observation(observed)
            except Exception:
                pass
        for purchase, extra in self.store.pending_extra_payments():
            try:
                payload = PaymentPayload.model_validate_json(extra["pending_payload"])
                if await self.chain.confirmed(payload, extra["transaction"]):
                    self.store.record_extra_payment(
                        purchase,
                        extra["payer"],
                        extra["nonce"],
                        extra["transaction"],
                        extra["amount"],
                    )
            except Exception:
                self.store.recovery_deferred(purchase, "additional_payment")
        for row in self.store.outstanding():
            purchase = row["id"]
            try:
                if row["state"] in ("SETTLING", "PAYMENT_UNKNOWN"):
                    payload = PaymentPayload.model_validate_json(row["payload"])
                    tx = row["tx"] or await self.chain.find_transaction(payload, from_block)
                    if tx and await self.chain.confirmed(payload, tx):
                        settlement = {
                            "success": True,
                            "transaction": tx,
                            "network": self.config.network,
                            "payer": row["payer"],
                            "amount": str(row["price"]),
                        }
                        self.store.transition(
                            purchase,
                            row["state"],
                            "FINALITY_PENDING",
                            tx=tx,
                            settlement=json.dumps(settlement),
                            liability=row["price"],
                            variable_fees=self.config.realized_payment_fee,
                        )
                    elif not tx:
                        proof = getattr(self.chain, "settlement_impossible", None)
                        if callable(proof) and await proof(payload):
                            self.store.transition(
                                purchase,
                                row["state"],
                                "PAYMENT_REJECTED",
                                supplier_cogs=0,
                                # Nonpayment proof does not prove a failed onchain attempt was free.
                                variable_fees=self.config.realized_payment_fee,
                                liability=0,
                            )
                await self.advance(purchase)
            except Exception:
                self.store.recovery_deferred(purchase, "purchase")


def create_app(service: SearchService) -> FastAPI:
    @asynccontextmanager
    async def service_lifespan(_app: FastAPI):
        from_block = service.config.recovery_from_block
        if (
            service.store.outstanding()
            or service.store.pending_extra_payments()
            or service.store.observed_payments()
        ) and not from_block:
            raise RuntimeError(
                "SEARCH_RECOVERY_FROM_BLOCK is required while purchases are unresolved"
            )
        if from_block:
            service.prepare_recovery()

        async def reconcile() -> None:
            while True:
                if from_block:
                    try:
                        await service.recover(from_block, startup=False)
                    except Exception:
                        # Unknown chain state stays frozen; the next read-only pass may resolve it.
                        pass
                await asyncio.sleep(10)

        task = asyncio.create_task(reconcile())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            facilitator_close = getattr(service.facilitator, "aclose", None)
            if callable(facilitator_close):
                await facilitator_close()
            for client in (
                getattr(service.chain, "client", None),
                getattr(service.supplier, "client", None),
            ):
                if client is not None and callable(getattr(client, "aclose", None)):
                    await client.aclose()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with service.store.writer_lease():
            async with service_lifespan(_app):
                yield

    app = FastAPI(title="Inferrail Job-Safe Web Search", lifespan=lifespan)
    app.state.search = service

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "network": service.config.network,
            "supplier": service.supplier.name,
        }

    @app.get("/.well-known/x402.json")
    async def manifest() -> dict[str, Any]:
        return {
            "name": SERVICE_NAME,
            "description": service.description,
            "resource": service.config.resource_url,
            "price_usd": usd(service.config.price),
            "network": service.config.network,
            "tags": SERVICE_TAGS,
            "input_schema": SearchRequest.model_json_schema(),
            "output_example": service.output_example,
            "output_schema": OUTPUT_SCHEMA,
            "payment_required": service.requirements.model_dump(by_alias=True),
            "extensions": service.extensions,
            "cache_ttl_seconds": service.config.cache_ttl,
            "job_token_ttl_seconds": service.config.job_ttl,
        }

    @app.post("/search")
    async def search(request: Request) -> JSONResponse:
        signature = request.headers.get("payment-signature")
        raw = await request.body()

        async def refused(status: int, error: str) -> JSONResponse:
            observed = await service.observe_payment(signature)
            details: dict[str, Any] = {"error": error, "charged_usd": "0"}
            if observed is not None and observed["state"] == "REFUNDED":
                details.update(
                    {"refunded_usd": usd(observed["amount"]), "financial_state": "RESOLVED"}
                )
            elif observed is not None and observed["state"] != "NOT_SETTLED":
                details.update(
                    {
                        "charged_usd": usd(observed["amount"])
                        if observed["state"] == "SETTLED"
                        else None,
                        "financial_state": "UNRESOLVED",
                        "payment_instruction": (
                            "Reuse the original signature; "
                            "never sign another payment for this request."
                        ),
                        "payment_nonce": observed["nonce"],
                        "transaction": observed["tx"],
                    }
                )
            return JSONResponse(status_code=status, content=details)

        if len(raw) > 16_384:
            return await refused(413, "body_too_large")
        # Discovery probes with an empty body must receive the complete 402 contract.
        if not raw or raw.strip() == b"{}":
            if not signature:
                return service.challenge()
        try:
            body = SearchRequest.model_validate_json(raw)
            return await service.handle(body, signature)
        except ValidationError:
            return await refused(422, "invalid_search_request")
        except Refused as exc:
            return await refused(409, str(exc))

    return app


def production_app() -> FastAPI:
    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient

    path = Path(os.environ["SEARCH_DB_PATH"]).resolve()
    if any(
        path.is_relative_to(root) for root in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
    ):
        raise ValueError("SEARCH_DB_PATH_requires_persistent_storage")
    if not path.is_file() and os.environ.get("SEARCH_ALLOW_NEW_DB") != "1":
        raise ValueError("refusing_new_database_without_initialization_approval")
    config = Config(
        recovery_from_block=os.environ.get("SEARCH_RECOVERY_FROM_BLOCK"),
        price=atomic(os.environ.get("SEARCH_PRICE_USD", "0.01")),
        fee_bound=atomic(os.environ.get("SEARCH_PAYMENT_FEE_BOUND_USD", "0.001")),
        minimum_margin=atomic(os.environ.get("SEARCH_MINIMUM_MARGIN_USD", "0.005")),
        risk_ceiling=atomic(os.environ.get("SEARCH_RISK_CEILING_USD", "15")),
        supplier_prepaid_capital=atomic(os.environ.get("SEARCH_SUPPLIER_PREPAID_CAPITAL_USD", "0")),
        pay_to=os.environ["SEARCH_PAY_TO"],
        resource_url=os.environ["SEARCH_RESOURCE_URL"],
        token_secret=os.environ["SEARCH_TOKEN_SECRET"].encode(),
        network=os.environ.get("SEARCH_NETWORK", "eip155:84532"),
        mainnet_approved=os.environ.get("SEARCH_MAINNET_APPROVED") == "1",
        supplier_rights_confirmed=os.environ.get("SEARCH_SUPPLIER_RIGHTS_CONFIRMED") == "1",
        realized_payment_fee=(
            0 if os.environ.get("SEARCH_NETWORK", "eip155:84532") == "eip155:84532" else None
        ),
    )
    if config.network == "eip155:8453" and os.environ.get("SEARCH_REALIZED_PAYMENT_FEE_USD"):
        raise ValueError("remove_static_mainnet_fee_and_reconcile_actual_per_purchase_fees")
    provider = os.environ.get("SEARCH_SUPPLIER", "fixture")
    if config.network == "eip155:84532":
        if provider != "fixture":
            raise ValueError("testnet_production_factory_requires_fixture_supplier")
        supplier = FixtureSearch()
    elif provider == "serpex":
        supplier = SerpexSearch(
            os.environ["SERPEX_API_KEY"], atomic(os.environ["SEARCH_SUPPLIER_CREDIT_USD"])
        )
    elif provider == "exa":
        supplier = ExaSearch(os.environ["EXA_API_KEY"])
    elif provider == "mojeek":
        if os.environ.get("SEARCH_MOJEEK_BUSINESS_TERMS_CONFIRMED") != "1":
            raise ValueError("mojeek_business_account_terms_required")
        supplier = MojeekBusinessSearch(
            os.environ["MOJEEK_API_KEY"],
            atomic(os.environ["SEARCH_MOJEEK_VERIFIED_UNIT_COST_USD"]),
        )
    else:
        raise ValueError("mainnet_requires_explicit_supplier_selection")
    if config.network == "eip155:8453":
        from .metrics import excluded_wallets

        excluded = excluded_wallets(Path(os.environ["SEARCH_EXCLUDED_WALLETS_PATH"]))
        if config.pay_to.lower() not in excluded:
            raise ValueError("mainnet_merchant_must_be_in_wallet_exclusions")
        if not config.recovery_from_block:
            raise ValueError("mainnet_requires_SEARCH_RECOVERY_FROM_BLOCK_before_first_acceptance")
    chain = ChainEvidence(os.environ["SEARCH_RPC_URL"], config.requirements(), finalized=True)
    facilitator = HTTPFacilitatorClient(create_facilitator_config())
    return create_app(SearchService(config, Store(path), facilitator, chain, supplier))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "hosted.job_safe_search.service:production_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8422")),
        log_level="info",
    )
