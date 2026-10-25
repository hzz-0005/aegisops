"""Exercise the ambiguous side effect path and manual observed-recovery reconciliation."""

import json
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import select
from verify_lab_executor import compose, latest_new_redis_incident

from aegisops.application.investigation.service import latest_report
from aegisops.application.remediation.approval import ApprovalDecisionInput, decide_approval
from aegisops.application.remediation.lab_executor import (
    LabDockerAdapter,
    LabReadiness,
    execute_authorized_redis,
    reconcile_recovery_observed,
)
from aegisops.application.remediation.policy import authorize_plan
from aegisops.application.remediation.restart_preconditions import RESTART_TARGETS, restart_identity
from aegisops.application.remediation.service import propose_plan
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    ResourceLeaseRecord,
)


class AmbiguousRecoveryAdapter(LabDockerAdapter):
    def _inspect_restart(self, _http: httpx.Client, action_type: str):
        info = json.loads(subprocess.check_output(
            ["docker", "inspect", RESTART_TARGETS[action_type][1]], text=True,
        ))[0]
        return restart_identity(info, action_type)

    def redis_healthy(self) -> bool:
        return False

    def restart_redis(self) -> None:
        compose("start", "redis")
        raise OSError("simulated lost Docker response after Redis started")

    def redis_started_at(self) -> datetime:
        value = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", "aegisops-redis-1"],
            text=True,
        ).strip()
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def verify_redis(self) -> LabReadiness:
        for _ in range(20):
            pong = subprocess.run(
                ["docker", "exec", "aegisops-redis-1", "redis-cli", "ping"],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip() == "PONG"
            try:
                payment = httpx.get("http://127.0.0.1:8002/health/ready", timeout=2)
                ready = payment.status_code == 200
            except httpx.HTTPError:
                ready = False
            result = LabReadiness(
                checked_at=datetime.now(UTC), redis_ping=pong, payment_ready=ready
            )
            if result.passed:
                return result
            time.sleep(1)
        return result


def main() -> None:
    settings = Settings(remediation_kill_switch=False)
    adapter = AmbiguousRecoveryAdapter()
    compose("stop", "lab-executor")
    try:
        assert httpx.get("http://127.0.0.1:8002/health/ready", timeout=5).status_code == 200
        started = datetime.now(UTC)
        compose("stop", "redis")
        incident_id = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = httpx.post(
                "http://127.0.0.1:8001/checkout",
                json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
                timeout=5,
            )
            assert response.status_code == 502
            incident_id = latest_new_redis_incident(settings, started)
            if incident_id is not None:
                break
            time.sleep(5)
        assert incident_id is not None
        report = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            report = latest_report(settings, settings.dev_tenant_id, incident_id)
            if report is not None:
                break
            time.sleep(2)
        assert report is not None and report.status == "SUPPORTED"
        proposal = propose_plan(settings, settings.dev_tenant_id, incident_id)
        decision = authorize_plan(settings, settings.dev_tenant_id, incident_id, "dev-operator")
        assert decision.outcome == "REQUIRE_APPROVAL" and decision.approval_id is not None
        approval = decide_approval(
            settings,
            settings.dev_tenant_id,
            decision.approval_id,
            ApprovalDecisionInput(
                expected_version=1,
                decision="APPROVE",
                reason="Verify ambiguous Lab recovery reconciliation",
            ),
            "dev-approver",
        )
        assert approval.status == "APPROVED"
        action_id = proposal.actions[0].id
        assert (
            execute_authorized_redis(settings, settings.dev_tenant_id, action_id, adapter)
            == "RECONCILIATION_REQUIRED"
        )
        with session_for(settings.database_url) as session:
            action = session.scalar(select(ActionRecord).where(ActionRecord.id == action_id))
            attempt = session.scalar(
                select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action_id)
            )
            lease = session.scalar(
                select(ResourceLeaseRecord).where(
                    ResourceLeaseRecord.resource_key == proposal.actions[0].snapshot.target
                )
            )
            assert action is not None and action.status == "RECONCILIATION_REQUIRED"
            assert attempt is not None and attempt.state == "RECONCILIATION_REQUIRED"
            assert lease is not None and lease.holder_action_id == action_id
        outcome = reconcile_recovery_observed(
            settings,
            settings.dev_tenant_id,
            action_id,
            adapter,
            operator_id="dev-operator",
            reason="Redis started after the attempt and Payment readiness recovered",
        )
        assert outcome == "RECOVERY_OBSERVED", outcome
        with session_for(settings.database_url) as session:
            action = session.scalar(select(ActionRecord).where(ActionRecord.id == action_id))
            attempt = session.scalar(
                select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action_id)
            )
            lease = session.scalar(
                select(ResourceLeaseRecord).where(
                    ResourceLeaseRecord.resource_key == proposal.actions[0].snapshot.target
                )
            )
            assert action is not None and action.status == "RECOVERY_OBSERVED"
            assert attempt is not None and attempt.state == "RECOVERY_OBSERVED"
            assert attempt.result["verification"]["redis_ping"] is True
            assert lease is not None and lease.holder_action_id is None
        print(f"PASS incident={incident_id} action={action_id} ambiguous recovery reconciled")
    finally:
        compose("start", "redis")
        compose("start", "lab-executor")


if __name__ == "__main__":
    main()
