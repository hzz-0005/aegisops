"""Real PostgreSQL concurrency/tenant gates using synthetic, isolated evaluation rows."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from aegisops.application.request_derived_signals import DETECTOR_VERSION, snapshot_digest
from aegisops.application.request_signal_scheduler import enqueue_late_windows, requeue_blocked_run
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    RequestFailureEventRecord as Event,
)
from aegisops.infrastructure.database.models import (
    RequestSignalDetectorRunRecord as Run,
)
from aegisops.infrastructure.database.models import (
    RequestSignalWindowRecord as Window,
)
from aegisops.infrastructure.database.models import (
    TenantRecord,
)


def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    tenant, other = uuid4(), uuid4()
    isolated = settings.model_copy(update={"dev_tenant_id": tenant})
    foreign = settings.model_copy(update={"dev_tenant_id": other})
    start = datetime(2026, 10, 10, tzinfo=UTC)
    source_id = uuid4()
    report: dict = {"scope": "synthetic_isolated_database_queue_gates_not_fault_accuracy"}

    def add_event(session, window_start):
        row = Event(id=uuid4(), tenant_id=tenant, source_event_id=uuid4(),
            service_name="queue-evaluation", trace_id=uuid4().hex, span_id=uuid4().hex[:16],
            failure_code="dependency_timeout", resource_name="dependency",
            occurred_at=window_start + timedelta(seconds=1))
        session.add(row)

    def add_window(session, window_start, key):
        session.add(Window(id=key, tenant_id=tenant, detector_version=DETECTOR_VERSION,
            window_start=window_start, window_end=window_start + timedelta(minutes=10),
            snapshot_event_ids=[], snapshot_digest=snapshot_digest([]), event_count=0,
            signal_count=0))

    try:
        with session_for(settings.database_url) as session, session.begin():
            for key in (tenant, other):
                session.add(TenantRecord(id=key, tenant_id=key, name=f"queue-eval-{key}"))
            add_window(session, start, source_id)
            add_event(session, start)
            add_event(session, start)
        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(enqueue_late_windows, [isolated, isolated]))
        assert sum(inserted) == 1
        assert enqueue_late_windows(foreign) == 0
        with session_for(settings.database_url) as session:
            first = session.scalar(select(Run).where(Run.tenant_id == tenant))
            assert first is not None and first.snapshot_event_ids is not None
            pinned = list(first.snapshot_event_ids)
            first_id = first.id
        with session_for(settings.database_url) as session, session.begin():
            add_event(session, start)
        assert enqueue_late_windows(isolated) == 1
        with session_for(settings.database_url) as session:
            row = session.get(Run, first_id)
            assert row is not None and row.snapshot_event_ids == pinned
            assert len(session.scalars(select(Run).where(Run.tenant_id == tenant)).all()) == 2
        report["concurrent_unique_enqueue"] = True
        report["foreign_tenant_excluded"] = True
        report["later_arrival_keeps_prior_members"] = True
        try:
            with session_for(settings.database_url) as session, session.begin():
                session.add(Run(id=uuid4(), tenant_id=other, detector_version="late-v1:foreign",
                    window_start=start, window_end=start + timedelta(minutes=10),
                    source_window_id=source_id, snapshot_event_ids=[], status="PENDING",
                    attempt_count=0, snapshot_complete=True))
        except IntegrityError:
            report["cross_tenant_source_fk_rejected"] = True
        else:
            raise AssertionError("cross-tenant source reference accepted")
        oversized_start = start + timedelta(minutes=10)
        healthy_start = start + timedelta(minutes=20)
        with session_for(settings.database_url) as session, session.begin():
            add_window(session, oversized_start, uuid4())
            for _ in range(1001):
                add_event(session, oversized_start)
            add_window(session, healthy_start, uuid4())
            add_event(session, healthy_start)
            add_event(session, healthy_start)
        assert enqueue_late_windows(isolated) == 2
        with session_for(settings.database_url) as session:
            blocked = session.scalar(select(Run).where(Run.tenant_id == tenant,
                                                       Run.window_start == oversized_start))
            assert blocked is not None and blocked.status == "BLOCKED"
            assert blocked.snapshot_complete is False
            blocked_id = blocked.id
            generation = blocked.version
        try:
            requeue_blocked_run(isolated, blocked_id, expected_generation=generation,
                                operator_id="evaluation", reason="验证不完整快照拒绝重排")
        except ValueError:
            report["incomplete_requeue_rejected"] = True
        else:
            raise AssertionError("incomplete snapshot was requeued")
        with session_for(settings.database_url) as session, session.begin():
            add_event(session, healthy_start)
        assert enqueue_late_windows(isolated) == 1
        report["blocked_window_does_not_starve_others"] = True
        report["passed"] = True
    except Exception as error:
        report.update(passed=False, error_code=type(error).__name__)
        raise
    finally:
        with session_for(settings.database_url) as session, session.begin():
            for model in (Run, Window, Event, TenantRecord):
                session.execute(delete(model).where(model.tenant_id.in_([tenant, other])))
        report["isolated_rows_removed"] = True
        path = Path(__file__).resolve().parents[1] / "eval" / (
            f"late-queue-gates-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**report, "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
