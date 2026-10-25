"""Manual request context never becomes unverified incident evidence."""

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.application import incident_request_evidence
from aegisops.application.incident_request_evidence import (
    AttachRequestEvidenceInput,
    RequestEvidenceNotFound,
    RequestEvidenceNotVerified,
    attach_request_evidence,
)
from aegisops.config import Settings


def body() -> AttachRequestEvidenceInput:
    return AttachRequestEvidenceInput(
        request_event_id=uuid4(), expected_incident_version=1, reason="核对业务失败请求"
    )


def test_attachment_requires_meaningful_reason() -> None:
    with pytest.raises(ValidationError):
        AttachRequestEvidenceInput(
            request_event_id=uuid4(), expected_incident_version=1, reason="   "
        )


@pytest.mark.parametrize("status", [None, "SOURCE_ASSERTED", "TRACE_NOT_READY"])
def test_unverified_event_stops_before_incident_mutation(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    def verification_result(settings: object, event_id: object) -> SimpleNamespace | None:
        return None if status is None else SimpleNamespace(provenance_status=status)

    monkeypatch.setattr(
        incident_request_evidence,
        "verify_request_event",
        verification_result,
    )
    with pytest.raises(RequestEvidenceNotFound if status is None else RequestEvidenceNotVerified):
        attach_request_evidence(
            cast(Settings, None), uuid4(), body(), operator_id="dev-operator"
        )
