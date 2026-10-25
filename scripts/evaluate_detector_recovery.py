"""Exercise scheduler recovery against real PostgreSQL in a disposable tenant."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from aegisops.application.request_derived_signals import (
    DETECTOR_VERSION,
    SignalWindowTooLarge,
    get_request_signal_window,
    materialize_closed_window,
)
from aegisops.application.request_signal_scheduler import (
    LEASE_MINUTES,
    claim_due_run,
    finish_run,
    latest_eligible_window,
    requeue_blocked_run,
)
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import RequestFailureEventRecord as Event
from aegisops.infrastructure.database.models import RequestSignalDetectorRunRecord as Run
from aegisops.infrastructure.database.models import RequestSignalWindowRecord as Window

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    tenant = uuid4()
    settings = settings.model_copy(update={"dev_tenant_id": tenant})
    now = datetime.now(UTC)
    start = latest_eligible_window(now)
    checks: dict[str, bool] = {}
    run_id = uuid4()
    try:
        with session_for(settings.database_url) as session, session.begin():
            session.add(Run(
                id=run_id, tenant_id=tenant, detector_version=DETECTOR_VERSION,
                window_start=start, window_end=start + timedelta(minutes=10),
                status="PENDING", attempt_count=0,
            ))
        first = claim_due_run(settings, now=now)
        assert first is not None and first.attempt_count == 1
        checks["active_lease_excludes_second_claim"] = claim_due_run(settings, now=now) is None
        takeover_at = now + timedelta(minutes=LEASE_MINUTES, seconds=1)
        second = claim_due_run(settings, now=takeover_at)
        assert second is not None and second.id == first.id and second.attempt_count == 2
        checks["expired_lease_is_reclaimed"] = True
        checks["stale_success_is_rejected"] = not finish_run(settings, first, now=takeover_at)
        checks["stale_failure_is_rejected"] = not finish_run(
            settings, first, error=RuntimeError("stale"), now=takeover_at,
        )
        assert finish_run(settings, second, error=TimeoutError(), now=takeover_at)
        checks["retry_waits_for_backoff"] = claim_due_run(
            settings, now=takeover_at + timedelta(seconds=29),
        ) is None
        third = claim_due_run(settings, now=takeover_at + timedelta(seconds=30))
        assert third is not None and third.attempt_count == 3
        checks["retry_reclaims_same_run"] = third.id == run_id
        assert finish_run(settings, third, now=takeover_at + timedelta(seconds=31))
        checks["finalized_run_not_reclaimed"] = claim_due_run(
            settings, now=takeover_at + timedelta(hours=1),
        ) is None
        with session_for(settings.database_url) as session, session.begin():
            session.add(Run(
                id=uuid4(), tenant_id=tenant, detector_version=DETECTOR_VERSION,
                window_start=start + timedelta(minutes=10),
                window_end=start + timedelta(minutes=20), status="PENDING", attempt_count=0,
            ))
        oversized = claim_due_run(settings, now=takeover_at)
        assert oversized is not None
        assert finish_run(settings, oversized, error=SignalWindowTooLarge(), now=takeover_at)
        with session_for(settings.database_url) as session:
            blocked = session.scalar(select(Run).where(Run.id == oversized.id))
            assert blocked is not None
            checks["oversized_window_blocks_without_retry"] = (
                blocked.status == "BLOCKED" and blocked.next_retry_at is None
            )
        operation = requeue_blocked_run(
            settings, oversized.id, expected_generation=1, operator_id="fixture-operator",
            reason="开发验收：新一代重试必须保留历史并拒绝旧领取者",
        )
        replacement = claim_due_run(settings, now=takeover_at)
        assert replacement is not None and replacement.generation == 2
        assert replacement.attempt_count == oversized.attempt_count == 1
        checks["old_generation_cannot_finish_matching_attempt"] = not finish_run(
            settings, oversized, now=takeover_at,
        )
        checks["requeue_is_audited"] = operation["old_error_code"] == "SignalWindowTooLarge"
        assert finish_run(settings, replacement, now=takeover_at)
        assert all(checks.values()), checks
        # Synthetic delivery fixture: this verifies immutable storage, not source or Tempo truth.
        assert materialize_closed_window(settings, start) == []
        before = get_request_signal_window(settings, start)
        assert before is not None
        event_id = uuid4()
        with session_for(settings.database_url) as session, session.begin():
            session.add(Event(
                id=event_id, tenant_id=tenant, source_event_id=uuid4(),
                service_name="fixture-service", trace_id=uuid4().hex, span_id=uuid4().hex[:16],
                failure_code="fixture_failure", resource_name="fixture-resource",
                occurred_at=start + timedelta(minutes=1),
            ))
        assert materialize_closed_window(settings, start) == []
        after = get_request_signal_window(settings, start)
        assert after is not None
        checks["late_delivery_is_visible"] = after.late_event_ids == [event_id]
        checks["late_delivery_preserves_snapshot"] = (
            after.snapshot_digest == before.snapshot_digest
            and after.event_count == before.event_count == 0
            and after.signal_count == 0
        )
        assert all(checks.values()), checks
    finally:
        with session_for(settings.database_url) as session, session.begin():
            session.execute(delete(Run).where(Run.tenant_id == tenant))
            session.execute(delete(Window).where(Window.tenant_id == tenant))
            session.execute(delete(Event).where(Event.tenant_id == tenant))
    report = {
        "captured_at": now.isoformat(), "checks": checks,
        "passed": all(checks.values()), "test_tenant_cleaned": True,
        "scope": (
            "真实 PostgreSQL 调度及模拟迟到入库测试；时间由参数推进，"
            "不证明 Tempo 故障恢复或实际进程崩溃。"
        ),
    }
    path = ROOT / "eval" / f"detector-recovery-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
