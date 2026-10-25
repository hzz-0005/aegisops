from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegisops.application.contracts import MergeIncidentsInput, SplitIncidentInput


def test_merge_requires_versioned_nonblank_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        MergeIncidentsInput(
            target_incident_id=uuid4(),
            expected_source_version=2,
            expected_target_version=3,
            reason="   ",
        )


def test_split_requires_selected_alert_and_reason() -> None:
    with pytest.raises(ValidationError):
        SplitIncidentInput(
            expected_version=2,
            alert_ids=[],
            title="Separate incident",
            reason="Verified different cause",
        )
    body = SplitIncidentInput(
        expected_version=2,
        alert_ids=[uuid4()],
        title="Separate incident",
        reason="  Verified different cause  ",
    )
    assert body.reason == "Verified different cause"
