"""Operator-attested, read-only Docker reconciliation of an uncertain Lab action."""

import argparse
from uuid import UUID

from aegisops.application.remediation.lab_executor import (
    LabDockerAdapter,
    reconcile_no_effect,
    reconcile_recovery_observed,
)
from aegisops.config import Profile, Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-id", required=True, type=UUID)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--outcome", choices=["no-effect", "recovery-observed"], default="no-effect"
    )
    args = parser.parse_args()
    settings = Settings()
    if settings.profile != Profile.DEV:
        raise RuntimeError("Lab reconciliation only runs in the dev profile")
    reconcile = (
        reconcile_no_effect
        if args.outcome == "no-effect"
        else reconcile_recovery_observed
    )
    result = reconcile(
        settings,
        settings.dev_tenant_id,
        args.action_id,
        LabDockerAdapter(),
        operator_id=args.operator_id,
        reason=args.reason,
    )
    print(result)
    expected = "FAILED_NO_EFFECT" if args.outcome == "no-effect" else "RECOVERY_OBSERVED"
    if result != expected:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
