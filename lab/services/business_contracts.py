"""Versioned Lab business assertions independent of the mutable inventory database."""

import json
import os
from pathlib import Path

CONTRACT_FILE = Path(
    os.getenv(
        "LAB_BUSINESS_CONTRACTS_FILE",
        str(Path(__file__).resolve().parents[1] / "config" / "business_contracts.json"),
    )
)
ASSERTIONS: list[dict[str, str]] = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))[
    "assertions"
]


def checkout_currency_assertion(sku: str) -> dict[str, str] | None:
    for assertion in ASSERTIONS:
        if assertion.get("checkout_sku") == sku and assertion.get("field") == "currency":
            return assertion
    return None
