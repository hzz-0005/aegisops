"""Verify a provider failure records an audit result and one bounded retry succeeds."""

import argparse
from unittest.mock import patch
from uuid import UUID

import httpx

from aegisops.application.investigation.model_review import ModelCommentary, request_model_review
from aegisops.application.investigation.service import latest_report
from aegisops.application.model_gateway import ModelGateway
from aegisops.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident-id", type=UUID, required=True)
    args = parser.parse_args()
    settings = Settings(
        llm_base_url="https://example.invalid/v1",
        llm_model="offline-test-model",
        llm_api_key="offline-test-key",
    )
    report = latest_report(settings, settings.dev_tenant_id, args.incident_id)
    assert report is not None and not report.is_stale and report.evidence
    valid = ModelCommentary(
        summary="The persisted alert supports the deterministic conclusion.",
        cited_evidence_ids=[report.evidence[0].id],
        uncertainties=[],
    )
    with patch.object(
        ModelGateway,
        "generate_structured",
        side_effect=[httpx.ConnectError("simulated provider outage"), valid],
    ):
        failed = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
        completed = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
    assert failed.status == "FAILED" and failed.error_code == "PROVIDER_UNAVAILABLE"
    assert completed.status == "COMPLETED" and completed.attempt_count == 2
    assert completed.id == failed.id
    print(f"PASS incident={args.incident_id} review={completed.id} bounded provider retry")


if __name__ == "__main__":
    main()
