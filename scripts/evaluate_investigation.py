"""Evaluate real Lab investigation evidence without changing an Incident or Action."""

import argparse
import json
import time
from pathlib import Path
from uuid import UUID

from aegisops.application.investigation.model_review import (
    LIMITATION_CODES,
    SYSTEM_INSTRUCTION,
    ModelCommentary,
)
from aegisops.application.model_gateway import ModelGateway
from aegisops.config import Settings
from scripts.model_provider_profiles import PROVIDERS, settings_for_provider

DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "eval" / "investigation_cases.json"
EXPECTED_CASES = {
    "PaymentRedisUnavailable": "redis_unavailable",
    "OrderDbPoolExhausted": "order_db_pool_exhaustion",
    "OrderBadDeployment": "bad_order_deployment",
}
SAFE_FACT_FIELDS = {
    "ALERT": {"alert_name", "service", "status"},
    "METRIC": {"peak_increase", "sample_count", "value", "increase"},
    "CHANGE": {"change_type", "from_ref", "to_ref"},
}


def validate_cases(data: dict) -> list[dict]:
    cases = data.get("cases")
    if data.get("schema_version") != 1 or not isinstance(cases, list):
        raise ValueError("evaluation requires version-1 Lab cases")
    if {case.get("scenario") for case in cases} != set(EXPECTED_CASES) or len(cases) != 3:
        raise ValueError("evaluation requires one case for each of the three Lab scenarios")
    for case in cases:
        payload = case["input"]
        if case["expected_hypothesis"] != EXPECTED_CASES[case["scenario"]]:
            raise ValueError("case ground truth does not match its Lab scenario")
        if UUID(case["source_report_id"]) != UUID(payload["report_id"]):
            raise ValueError("case source report ID does not match model input")
        if payload["status"] != case["report_status"]:
            raise ValueError("case report status does not match model input")
        if len(json.dumps(payload, ensure_ascii=False)) > 8000:
            raise ValueError("case exceeds model input budget")
        allowed_limitations = {code for _, code in LIMITATION_CODES} | {"OTHER_LIMITATION"}
        if not set(payload["limitations"]) <= allowed_limitations:
            raise ValueError("case contains an unsanitized limitation")
        evidence = payload["evidence"]
        if not 1 <= len(evidence) <= 24:
            raise ValueError("case requires 1 to 24 evidence items")
        for item in evidence:
            allowed = SAFE_FACT_FIELDS.get(item["source_type"], set())
            if not isinstance(item["safe_facts"], dict) or not set(item["safe_facts"]) <= allowed:
                raise ValueError("case contains an unsafe evidence field")
    return cases


def evaluate_case(case: dict, gateway: ModelGateway | None) -> dict:
    payload = case["input"]
    evidence = payload["evidence"]
    evidence_ids = [UUID(item["id"]) for item in evidence]
    baseline_pass = (
        case["report_status"] == "SUPPORTED"
        and case["reported_hypothesis"] == case["expected_hypothesis"]
        and payload["primary_hypothesis"] == case["expected_hypothesis"]
        and len(evidence_ids) > 0
        and len(evidence_ids) == len(set(evidence_ids))
    )
    result = {
        "scenario": case["scenario"],
        "expected_hypothesis": case["expected_hypothesis"],
        "baseline_pass": baseline_pass,
        "evidence_count": len(evidence_ids),
        "historical_report_stale": case["report_stale_after_resolution"],
    }
    if gateway is not None:
        started = time.perf_counter()
        commentary = gateway.generate_structured(
            ModelCommentary,
            system_instruction=SYSTEM_INSTRUCTION,
            user_payload=json.dumps(payload, ensure_ascii=False),
        )
        citation_pass = set(commentary.cited_evidence_ids) <= set(evidence_ids)
        result.update(
            {
                "model_citation_pass": citation_pass,
                "model_citation_count": len(commentary.cited_evidence_ids),
                "model_latency_ms": round((time.perf_counter() - started) * 1000),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--with-model", action="store_true")
    parser.add_argument("--provider", choices=sorted(PROVIDERS))
    parser.add_argument("--list-providers", action="store_true")
    args = parser.parse_args()
    if args.list_providers:
        for name, profile in PROVIDERS.items():
            print(f"{name}: {profile.model} ({profile.key_env})")
        return
    if args.provider and not args.with_model:
        parser.error("--provider requires --with-model")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    cases = validate_cases(data)
    settings = (
        (settings_for_provider(args.provider) if args.provider else Settings())
        if args.with_model
        else None
    )
    gateway = ModelGateway(settings) if settings is not None else None
    results = [evaluate_case(case, gateway) for case in cases]
    passed = all(row["baseline_pass"] and row.get("model_citation_pass", True) for row in results)
    print(
        json.dumps(
            {
                "passed": passed,
                "provider": args.provider if args.with_model else None,
                "model": settings.llm_model if settings is not None else None,
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
