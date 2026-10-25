"""Read persisted outcomes from explicitly selected real recovery experiments."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import ActionAttemptRecord, ActionRecord


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action_ids", nargs="+", type=UUID)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    results = []
    with session_for(settings.database_url) as session:
        for ident in args.action_ids:
            action = session.scalar(
                select(ActionRecord).where(
                    ActionRecord.tenant_id == settings.dev_tenant_id, ActionRecord.id == ident
                )
            )
            attempts = session.scalars(
                select(ActionAttemptRecord).where(
                    ActionAttemptRecord.tenant_id == settings.dev_tenant_id,
                    ActionAttemptRecord.action_id == ident,
                )
            ).all()
            assert action is not None and action.status == "SUCCEEDED"
            assert len(attempts) == 1 and attempts[0].state == "SUCCEEDED"
            assert attempts[0].fencing_token > 0
            verification = attempts[0].result["verification"]
            assert verification["payment_ready"] is True
            assert verification.get("redis_ping", verification.get("order_ready")) is True
            results.append(
                {
                    "action_id": str(ident),
                    "incident_id": str(action.incident_id),
                    "action_type": action.snapshot["action_type"],
                    "status": action.status,
                    "attempt_count": len(attempts),
                    "verification": verification,
                }
            )
    now = datetime.now(UTC)
    path = Path("eval") / f"restart-gate-audit-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(
        json.dumps(
            {
                "captured_at": now.isoformat(),
                "passed": True,
                "results": results,
                "scope": "Known Redis and DB-pool recovery regression, not blind testing",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"passed": True, "actions": len(results), "report": str(path)}))


if __name__ == "__main__":
    main()
