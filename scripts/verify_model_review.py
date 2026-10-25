"""Persist a model review with a fake provider; never send Lab data to an external API."""

import argparse
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import func, select

from aegisops.application.investigation.model_review import (
    ModelCommentary,
    get_model_review,
    request_model_review,
)
from aegisops.application.investigation.service import latest_report
from aegisops.application.model_gateway import ModelGateway
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentTimelineRecord


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

    def fake_generate(self, output_type, *, system_instruction, user_payload):
        assert output_type is ModelCommentary
        assert "tools" not in user_payload
        return ModelCommentary(
            summary="The cited persisted evidence supports the deterministic investigation.",
            cited_evidence_ids=[report.evidence[0].id],
            uncertainties=["This commentary does not authorize remediation."],
        )

    with patch.object(ModelGateway, "generate_structured", fake_generate):
        first = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
        second = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
    assert first.status == "COMPLETED" and first.id == second.id
    assert first.attempt_count == 1 and first.cited_evidence_ids == [report.evidence[0].id]
    assert get_model_review(settings, settings.dev_tenant_id, args.incident_id) == first
    with session_for(settings.database_url) as session:
        count = session.scalar(
            select(func.count())
            .select_from(IncidentTimelineRecord)
            .where(
                IncidentTimelineRecord.tenant_id == settings.dev_tenant_id,
                IncidentTimelineRecord.incident_id == args.incident_id,
                IncidentTimelineRecord.event_type == "MODEL_REVIEWED",
            )
        )
    assert count == 1
    print(f"PASS incident={args.incident_id} review={first.id} offline bounded model review")


if __name__ == "__main__":
    main()
