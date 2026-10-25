"""Poll authorized Lab actions; never retry an uncertain side effect."""

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aegisops.application.remediation.lab_executor import (
    LabDockerAdapter,
    _finish,
    execute_authorized_lab,
)
from aegisops.config import Profile, Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import ActionAttemptRecord, ActionRecord
from apps.lab_executor.runtime_metrics import start_observer

logger = logging.getLogger(__name__)


def run_once(settings: Settings, adapter: LabDockerAdapter) -> None:
    with session_for(settings.database_url) as session:
        stale = session.execute(
            select(
                ActionAttemptRecord.tenant_id, ActionAttemptRecord.action_id, ActionAttemptRecord.id
            ).where(
                ActionAttemptRecord.state == "STARTED",
                ActionAttemptRecord.started_at < datetime.now(UTC) - timedelta(minutes=2),
            )
        ).all()
        pending = session.execute(
            select(ActionRecord.tenant_id, ActionRecord.id).where(
                ActionRecord.status == "AUTHORIZED",
                ActionRecord.snapshot["action_type"].astext.in_(
                    ["restart_cache", "restart_service", "rollback_service"]
                ),
            )
        ).all()
    for tenant_id, action_id, attempt_id in stale:
        if _finish(
            settings,
            tenant_id,
            action_id,
            attempt_id,
            status="RECONCILIATION_REQUIRED",
            reason="executor stopped before confirming the side effect",
        ):
            logger.warning("action %s requires manual reconciliation", action_id)
    for tenant_id, action_id in pending:
        outcome = execute_authorized_lab(settings, tenant_id, action_id, adapter)
        if outcome != "SKIPPED_NOT_AUTHORIZED":
            logger.info("action %s: %s", action_id, outcome)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.profile != Profile.DEV:
        raise RuntimeError("Lab executor only runs in the dev profile")
    start_observer()
    adapter = LabDockerAdapter()
    while True:
        try:
            run_once(settings, adapter)
        except Exception:
            logger.exception("Lab executor poll failed")
        time.sleep(3)


if __name__ == "__main__":
    main()
