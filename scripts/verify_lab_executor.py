"""Real Redis outage through alert, investigation, policy, approval and fenced recovery."""

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select

from aegisops.application.investigation.service import latest_report
from aegisops.application.remediation.approval import ApprovalDecisionInput, decide_approval
from aegisops.application.remediation.policy import authorize_plan
from aegisops.application.remediation.service import propose_plan
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    AlertRecord,
    IncidentAlertRecord,
    ResourceLeaseRecord,
)

ROOT = Path(__file__).resolve().parents[1]


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def latest_new_redis_incident(settings: Settings, since: datetime):
    with session_for(settings.database_url) as session:
        return session.scalar(
            select(IncidentAlertRecord.incident_id)
            .join(
                AlertRecord,
                (AlertRecord.tenant_id == IncidentAlertRecord.tenant_id)
                & (AlertRecord.id == IncidentAlertRecord.alert_id),
            )
            .where(
                AlertRecord.tenant_id == settings.dev_tenant_id,
                AlertRecord.payload["alertname"].astext == "PaymentRedisUnavailable",
                AlertRecord.occurred_at >= since,
            )
            .order_by(AlertRecord.occurred_at.desc())
            .limit(1)
        )


def main() -> None:
    settings = Settings(remediation_kill_switch=False)
    with httpx.Client(timeout=5) as http:
        assert http.get("http://127.0.0.1:8002/health/ready").status_code == 200
        started = datetime.now(UTC)
        compose("stop", "redis")
        try:
            deadline = time.monotonic() + 180
            incident_id = None
            while time.monotonic() < deadline:
                failed = http.post(
                    "http://127.0.0.1:8001/checkout",
                    json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
                )
                assert failed.status_code == 502
                incident_id = latest_new_redis_incident(settings, started)
                if incident_id is not None:
                    break
                time.sleep(5)
            assert incident_id is not None, "new Redis alert did not create an incident"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                report = latest_report(settings, settings.dev_tenant_id, incident_id)
                if report is not None:
                    break
                time.sleep(2)
            assert report is not None and report.status == "SUPPORTED"
            assert report.primary_hypothesis == "redis_unavailable"
            proposal = propose_plan(settings, settings.dev_tenant_id, incident_id)
            assert proposal.actions[0].snapshot.action_type == "restart_cache"
            decision = authorize_plan(settings, settings.dev_tenant_id, incident_id, "dev-operator")
            assert decision.outcome == "REQUIRE_APPROVAL" and decision.approval_id is not None
            approval = decide_approval(
                settings,
                settings.dev_tenant_id,
                decision.approval_id,
                ApprovalDecisionInput(
                    expected_version=1,
                    decision="APPROVE",
                    reason="Approve scoped Lab Redis restart during verified outage",
                ),
                "dev-approver",
            )
            assert approval.status == "APPROVED"
            action_id = proposal.actions[0].id
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                with session_for(settings.database_url) as session:
                    action = session.scalar(
                        select(ActionRecord).where(ActionRecord.id == action_id)
                    )
                    if action is not None and action.status in {
                        "SUCCEEDED",
                        "RECONCILIATION_REQUIRED",
                        "BLOCKED",
                    }:
                        break
                time.sleep(2)
            assert action is not None and action.status == "SUCCEEDED", (
                action.status if action else None
            )
            assert http.get("http://127.0.0.1:8002/health/ready").status_code == 200
            with session_for(settings.database_url) as session:
                attempts = session.scalars(
                    select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action_id)
                ).all()
                assert len(attempts) == 1 and attempts[0].state == "SUCCEEDED"
                assert attempts[0].fencing_token > 0
                assert attempts[0].result["verification"]["redis_ping"] is True
                assert attempts[0].result["verification"]["payment_ready"] is True
                lease = session.scalar(
                    select(ResourceLeaseRecord).where(
                        ResourceLeaseRecord.resource_key == proposal.actions[0].snapshot.target
                    )
                )
                assert lease is not None and lease.holder_action_id is None
                count = session.scalar(
                    select(func.count())
                    .select_from(ActionAttemptRecord)
                    .where(ActionAttemptRecord.action_id == action_id)
                )
                assert count == 1
            print(f"PASS incident={incident_id} action={action_id} fenced Redis recovery")
        finally:
            compose("start", "redis")


if __name__ == "__main__":
    main()
