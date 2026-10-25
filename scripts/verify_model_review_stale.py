"""Recover a stale reserved model call without allowing its old result to win."""

import argparse
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from aegisops.application.investigation.model_review import (
    PROMPT_VERSION,
    ModelCommentary,
    request_model_review,
)
from aegisops.application.investigation.service import latest_report
from aegisops.application.model_gateway import ModelGateway
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import InvestigationModelReviewRecord


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
    with session_for(settings.database_url) as session, session.begin():
        session.add(
            InvestigationModelReviewRecord(
                id=uuid4(),
                tenant_id=settings.dev_tenant_id,
                report_id=report.id,
                status="RUNNING",
                model_provider="openai-compatible",
                model_name="offline-test-model",
                prompt_version=PROMPT_VERSION,
                input_hash="0" * 64,
                attempt_count=1,
                cited_evidence_ids=[],
                uncertainties=[],
                updated_at=datetime.now(UTC) - timedelta(minutes=3),
            )
        )
    valid = ModelCommentary(
        summary="A stale reservation was reclaimed and the evidence citation was checked.",
        cited_evidence_ids=[report.evidence[0].id],
        uncertainties=[],
    )
    with patch.object(ModelGateway, "generate_structured", return_value=valid):
        completed = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
    assert completed.status == "COMPLETED" and completed.attempt_count == 2
    assert completed.input_hash != "0" * 64
    print(f"PASS incident={args.incident_id} review={completed.id} stale call reclaimed")


if __name__ == "__main__":
    main()
