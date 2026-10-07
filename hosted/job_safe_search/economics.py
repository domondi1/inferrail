"""Recognize contribution only from resolved financial records."""

from __future__ import annotations

from typing import Any

from .contract import usd


def financial_state(row: dict[str, Any]) -> dict[str, Any]:
    paid = row["state"] in (
        "SUPPLIER_INFLIGHT",
        "SUPPLIER_UNKNOWN",
        "SERVICE_FAILED",
        "DELIVERED",
        "RESOLVED",
    )
    resolved = (
        row["state"] in ("DELIVERED", "RESOLVED")
        and row["supplier_cogs"] is not None
        and row["variable_fees"] is not None
        and row["liability"] == 0
        and row["credits"] == 0
        and row.get("extra_payment_liability", 0) == 0
    )
    no_charge = row["state"] == "PAYMENT_REJECTED"
    margin = (
        row["price"] - row["supplier_cogs"] - row["refunds"] - row["credits"] - row["variable_fees"]
        if resolved
        else 0
        if no_charge
        else None
    )
    return {
        "settled_revenue": usd(row["price"]) if paid else "0" if no_charge else None,
        "recognized_revenue": usd(row["price"]) if resolved else "0" if no_charge else None,
        "supplier_cogs": usd(row["supplier_cogs"]),
        "refunds": usd(row["refunds"]),
        "credits": usd(row["credits"]),
        "variable_fees": usd(row["variable_fees"]),
        "unresolved_liability": usd(row["liability"] + row.get("extra_payment_liability", 0)),
        "realized_margin": usd(margin),
        "resolved": resolved or no_charge,
    }
