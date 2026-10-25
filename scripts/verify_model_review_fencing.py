"""Prove a late first provider response cannot overwrite a reclaimed review."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import select

from aegisops.application.investigation.model_review import (
    ModelCommentary,
    request_model_review,
)
from aegisops.application.investigation.service import latest_report
from aegisops.application.model_gateway import ModelGateway
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    IncidentTimelineRecord,
    InvestigationModelReviewRecord,
)


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
    first_started = Event()
    release_first = Event()
    call_lock = Lock()
    call_count = 0

    def fake_call(*_args, **_kwargs) -> ModelCommentary:
        nonlocal call_count
        with call_lock:
            call_count += 1
            number = call_count
        if number == 1:
            first_started.set()
            if not release_first.wait(timeout=15):
                raise RuntimeError("first simulated provider call timed out")
        return ModelCommentary(
            summary=f"provider response {number}",
            cited_evidence_ids=[report.evidence[0].id],
            uncertainties=[],
        )

    with patch.object(ModelGateway, "generate_structured", side_effect=fake_call):
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_future = pool.submit(
                request_model_review, settings, settings.dev_tenant_id, args.incident_id
            )
            assert first_started.wait(timeout=10)
            with session_for(settings.database_url) as session, session.begin():
                row = session.scalar(
                    select(InvestigationModelReviewRecord)
                    .where(
                        InvestigationModelReviewRecord.tenant_id == settings.dev_tenant_id,
                        InvestigationModelReviewRecord.report_id == report.id,
                    )
                    .with_for_update()
                )
                assert row is not None and row.status == "RUNNING" and row.attempt_count == 1
                row.updated_at = datetime.now(UTC) - timedelta(minutes=3)
            try:
                second = request_model_review(settings, settings.dev_tenant_id, args.incident_id)
            finally:
                release_first.set()
            first = first_future.result(timeout=10)
    assert call_count == 2
    assert second.status == "COMPLETED" and second.attempt_count == 2
    assert first.summary == second.summary == "provider response 2"
    with session_for(settings.database_url) as session:
        events = session.scalars(
            select(IncidentTimelineRecord).where(
                IncidentTimelineRecord.tenant_id == settings.dev_tenant_id,
                IncidentTimelineRecord.incident_id == args.incident_id,
                IncidentTimelineRecord.event_type == "MODEL_REVIEWED",
            )
        ).all()
    assert len(events) == 1
    print(f"PASS incident={args.incident_id} late response fenced, one review event")


if __name__ == "__main__":
    main()
