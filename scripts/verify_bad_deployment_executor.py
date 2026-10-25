"""Real bad Order deployment through governance and the one-shot Lab rollback adapter."""

import argparse
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import select

from aegisops.application.investigation.service import latest_report
from aegisops.application.remediation.approval import ApprovalDecisionInput, decide_approval
from aegisops.application.remediation.policy import authorize_plan
from aegisops.application.remediation.service import propose_plan
from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    ResourceLeaseRecord,
)
from lab.scenarios.bad_deployment import (
    API,
    OPERATOR_TOKEN,
    ORDER,
    ROOT,
    await_incident,
    await_metric_samples,
    await_revision,
    checkout,
    deploy,
    deployment_fingerprint,
)


def wait_for_report(settings: Settings, incident_id) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        report = latest_report(settings, settings.dev_tenant_id, incident_id)
        if report is not None:
            assert report.status == "SUPPORTED"
            assert report.primary_hypothesis == "bad_order_deployment"
            return
        time.sleep(2)
    raise RuntimeError("bad deployment investigation did not finish")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate-lost-response", action="store_true")
    args = parser.parse_args()
    settings = Settings(remediation_kill_switch=False)
    change_id = f"lab-order-bad-{uuid4().hex[:12]}"
    with httpx.Client(timeout=10) as client:
        baseline = checkout(client)
        assert baseline.status_code == 200
        before = deployment_fingerprint()
        assert before["bad_deployment"] is False
        started = datetime.now(UTC)
        if args.simulate_lost_response:
            subprocess.run(["docker", "compose", "stop", "lab-executor"], cwd=ROOT, check=True)
        try:
            deploy(change_id)
            await_revision(client, change_id, True)
            after = deployment_fingerprint()
            change = client.post(
                f"{API}/change-events",
                headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
                json={
                    "source": "lab-deployment",
                    "external_id": change_id,
                    "service": "order-service",
                    "environment": "lab",
                    "change_type": "DEPLOYMENT",
                    "from_ref": before["change_id"],
                    "to_ref": change_id,
                    "deployment": {"before": before, "after": after},
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            assert change.status_code == 201, change.text
            for _ in range(2):
                assert checkout(client).status_code == 500
            await_metric_samples(client, change_id)
            for _ in range(2):
                assert checkout(client).status_code == 500
            incident_id = await_incident(change_id, started)
            wait_for_report(settings, incident_id)
            proposal = propose_plan(settings, settings.dev_tenant_id, incident_id)
            action = proposal.actions[0]
            assert action.snapshot.action_type == "rollback_service"
            assert action.snapshot.parameters["current_config_hash"] == after[
                "compose_config_hash"
            ]
            assert action.snapshot.parameters["target_config_hash"] == before[
                "compose_config_hash"
            ]
            decision = authorize_plan(settings, settings.dev_tenant_id, incident_id, "dev-operator")
            assert decision.outcome == "REQUIRE_APPROVAL" and decision.approval_id is not None
            approval = decide_approval(
                settings,
                settings.dev_tenant_id,
                decision.approval_id,
                ApprovalDecisionInput(
                    expected_version=1,
                    decision="APPROVE",
                    reason="Approve scoped rollback to the observed healthy Order baseline",
                ),
                "dev-approver",
            )
            assert approval.status == "APPROVED"
            if args.simulate_lost_response:
                subprocess.run(
                    [
                        "docker", "compose", "run", "--rm", "--no-deps", "lab-executor",
                        "python", "-m", "apps.lab_executor.verify_rollback_uncertain",
                        "--action-id", str(action.id),
                    ],
                    cwd=ROOT,
                    check=True,
                )
            expected_status = (
                "RECOVERY_OBSERVED" if args.simulate_lost_response else "SUCCEEDED"
            )
            action_status = None
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                with session_for(settings.database_url) as session:
                    row = session.scalar(select(ActionRecord).where(ActionRecord.id == action.id))
                    action_status = row.status if row else None
                if action_status in {
                    "SUCCEEDED",
                    "RECOVERY_OBSERVED",
                    "BLOCKED",
                    "PRECONDITION_FAILED",
                    "RECONCILIATION_REQUIRED",
                }:
                    break
                time.sleep(2)
            assert action_status == expected_status, (
                f"rollback action={action.id} status={action_status}"
            )
            await_revision(client, "baseline", False)
            actual = deployment_fingerprint()
            assert actual == before
            with session_for(settings.database_url) as session:
                attempts = session.scalars(
                    select(ActionAttemptRecord).where(ActionAttemptRecord.action_id == action.id)
                ).all()
                assert len(attempts) == 1 and attempts[0].state == expected_status
                assert attempts[0].fencing_token > 0
                verification = attempts[0].result["verification"]
                assert verification["target_fingerprint_matches"] is True
                assert verification["baseline_deployed"] is True
                assert verification["order_ready"] is True
                assert verification["payment_ready"] is True
                lease = session.scalar(
                    select(ResourceLeaseRecord).where(
                        ResourceLeaseRecord.resource_key == action.snapshot.target
                    )
                )
                assert lease is not None and lease.holder_action_id is None
            assert checkout(client).status_code == 200
            print(
                f"PASS incident={incident_id} action={action.id} "
                f"governed Order rollback status={expected_status}"
            )
        finally:
            try:
                try:
                    active = client.get(f"{ORDER}/deployment")
                    restored = active.status_code == 200 and active.json() == {
                        "change_id": "baseline",
                        "bad_deployment": False,
                    }
                except (httpx.HTTPError, ValueError):
                    restored = False
                if not restored:
                    deploy(None)
                    await_revision(client, "baseline", False)
            finally:
                if args.simulate_lost_response:
                    subprocess.run(
                        ["docker", "compose", "up", "-d", "--no-deps", "lab-executor"],
                        cwd=ROOT,
                        check=True,
                    )


if __name__ == "__main__":
    main()
