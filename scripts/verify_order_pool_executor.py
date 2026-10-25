"""Real Order DB pool fault through alert, investigation, approval and fenced restart."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import select

from aegisops.application.investigation.service import latest_report
from aegisops.application.remediation.approval import (
    ApprovalDecisionInput,
    ApprovalRejected,
    decide_approval,
)
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

ORDER = "http://127.0.0.1:8003"
GATEWAY = "http://127.0.0.1:8001"
FAULT_HEADERS = {"x-lab-fault-token": os.getenv("LAB_FAULT_TOKEN", "aegisops_lab_fault_dev_only")}


def hold_connection() -> None:
    try:
        httpx.post(
            f"{ORDER}/lab/faults/db-pool/hold",
            params={"seconds": 180},
            headers=FAULT_HEADERS,
            timeout=200,
        )
    except httpx.HTTPError:
        # A successful service restart intentionally closes the held request.
        pass


def await_pool_exhaustion() -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{ORDER}/lab/faults/db-pool/stats", headers=FAULT_HEADERS, timeout=3
        )
        response.raise_for_status()
        stats = response.json()
        if stats.get("pool_size") == 2 and stats.get("pool_available", 0) == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Order DB pool did not become exhausted")


def latest_new_incident(settings: Settings, since: datetime):
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
                AlertRecord.payload["alertname"].astext == "OrderDbPoolExhausted",
                AlertRecord.occurred_at >= since,
            )
            .order_by(AlertRecord.occurred_at.desc())
            .limit(1)
        )


def main() -> None:
    settings = Settings(remediation_kill_switch=False)
    assert httpx.get(f"{ORDER}/health/ready", timeout=5).status_code == 200
    started = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holds = [pool.submit(hold_connection) for _ in range(2)]
        await_pool_exhaustion()
        assert httpx.get(f"{ORDER}/health/ready", timeout=5).status_code == 503
        incident_id = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            failed = httpx.post(
                f"{GATEWAY}/checkout",
                json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
                timeout=8,
            )
            assert failed.status_code == 503, failed.text
            incident_id = latest_new_incident(settings, started)
            if incident_id is not None:
                break
            time.sleep(4)
        assert incident_id is not None, "pool alert did not create an Incident"
        report = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            report = latest_report(settings, settings.dev_tenant_id, incident_id)
            if report is not None:
                break
            time.sleep(2)
        assert report is not None and report.status == "SUPPORTED"
        assert report.primary_hypothesis == "order_db_pool_exhaustion"
        proposal = propose_plan(settings, settings.dev_tenant_id, incident_id)
        assert proposal.actions[0].snapshot.action_type == "restart_service"
        decision = authorize_plan(settings, settings.dev_tenant_id, incident_id, "dev-operator")
        assert decision.outcome == "REQUIRE_APPROVAL" and decision.approval_id is not None
        try:
            decide_approval(
                settings, settings.dev_tenant_id, decision.approval_id,
                ApprovalDecisionInput(expected_version=1, decision="APPROVE",
                                      reason="开发验收：禁止操作员自批"),
                "dev-operator",
            )
        except ApprovalRejected as error:
            assert "own request" in str(error)
        else:
            raise AssertionError("requester approved their own action")
        approval = decide_approval(
            settings,
            settings.dev_tenant_id,
            decision.approval_id,
            ApprovalDecisionInput(
                expected_version=1,
                decision="APPROVE",
                reason="Approve scoped Order restart for verified pool exhaustion",
            ),
            "dev-approver",
        )
        assert approval.status == "APPROVED"
        action_id = proposal.actions[0].id
        action = None
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            with session_for(settings.database_url) as session:
                action = session.scalar(select(ActionRecord).where(ActionRecord.id == action_id))
                if action is not None and action.status in {
                    "SUCCEEDED",
                    "BLOCKED",
                    "PRECONDITION_FAILED",
                    "RECONCILIATION_REQUIRED",
                }:
                    break
            time.sleep(2)
        assert action is not None and action.status == "SUCCEEDED", (
            action.status if action else None
        )
        assert httpx.get(f"{ORDER}/health/ready", timeout=5).status_code == 200
        recovered = httpx.post(
            f"{GATEWAY}/checkout",
            json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
            timeout=8,
        )
        assert recovered.status_code == 200, "pool restart did not restore business checkout"
        with session_for(settings.database_url) as session:
            attempts = session.scalars(
                select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action_id)
            ).all()
            assert len(attempts) == 1 and attempts[0].state == "SUCCEEDED"
            assert attempts[0].fencing_token > 0
            assert attempts[0].result["verification"]["order_ready"] is True
            assert attempts[0].result["verification"]["payment_ready"] is True
            lease = session.scalar(
                select(ResourceLeaseRecord).where(
                    ResourceLeaseRecord.resource_key == proposal.actions[0].snapshot.target
                )
            )
            assert lease is not None and lease.holder_action_id is None
        for hold in holds:
            hold.result(timeout=5)
    print(f"PASS incident={incident_id} action={action_id} fenced Order pool recovery")


if __name__ == "__main__":
    main()
