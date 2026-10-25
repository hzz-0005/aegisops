"""Lab-only injection: Docker rollback succeeds but its response is lost."""

import argparse
from unittest.mock import patch
from uuid import UUID

import httpx

from aegisops.application.remediation.lab_executor import (
    LabDockerAdapter,
    execute_authorized_lab,
    reconcile_recovery_observed,
)
from aegisops.application.remediation.lab_rollback import LabOrderRollbackAdapter
from aegisops.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-id", type=UUID, required=True)
    args = parser.parse_args()
    settings = Settings()
    original = LabOrderRollbackAdapter.rollback

    def lose_response(self, snapshot, action_id):
        original(self, snapshot, action_id)
        raise httpx.ReadError("simulated lost response after Order container was recreated")

    with patch.object(LabOrderRollbackAdapter, "rollback", lose_response):
        outcome = execute_authorized_lab(
            settings, settings.dev_tenant_id, args.action_id, LabDockerAdapter()
        )
    assert outcome == "RECONCILIATION_REQUIRED", outcome
    observed = reconcile_recovery_observed(
        settings,
        settings.dev_tenant_id,
        args.action_id,
        LabDockerAdapter(),
        operator_id="dev-operator",
        reason="Observed approved baseline after the Docker response was lost",
    )
    assert observed == "RECOVERY_OBSERVED", observed
    print(f"PASS action={args.action_id} uncertain rollback reconciled without retry")


if __name__ == "__main__":
    main()
