"""Read-only continuity audit across all persisted detector windows."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from aegisops.application.request_derived_signals import DETECTOR_VERSION, WINDOW_MINUTES
from aegisops.application.request_signal_scheduler import latest_eligible_window
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    RequestSignalDetectorRunRecord as Run,
)
from aegisops.infrastructure.database.models import (
    RequestSignalWindowRecord as Window,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    now = datetime.now(UTC)
    with session_for(settings.database_url) as session:
        runs = session.scalars(select(Run).where(
            Run.tenant_id == settings.dev_tenant_id,
            Run.detector_version == DETECTOR_VERSION,
        ).order_by(Run.window_start)).all()
        windows = session.scalars(select(Window).where(
            Window.tenant_id == settings.dev_tenant_id,
            Window.detector_version == DETECTOR_VERSION,
        )).all()
        snapshots = {row.window_start: row for row in windows}
        starts = {row.window_start for row in runs}
        missing = []
        cursor = runs[0].window_start if runs else latest_eligible_window(now)
        while cursor <= latest_eligible_window(now):
            if cursor not in starts:
                missing.append(cursor.isoformat())
            cursor += timedelta(minutes=WINDOW_MINUTES)
        inconsistent = [row.window_start.isoformat() for row in runs
                        if row.status == "FINALIZED" and row.window_start not in snapshots]
        pending = [row for row in runs if row.status != "FINALIZED"]
        report = {
            "captured_at": now.isoformat(),
            "detector_version": DETECTOR_VERSION,
            "run_count": len(runs),
            "status_counts": dict(Counter(row.status for row in runs)),
            "attempt_counts": dict(Counter(row.attempt_count for row in runs)),
            "attempt_counts_scope": "current_generation",
            "lifetime_attempt_counts": dict(Counter(
                row.attempt_count + sum(op["old_attempt_count"] for op in row.requeue_history)
                for row in runs
            )),
            "requeued_run_count": sum(bool(row.requeue_history) for row in runs),
            "first_registered_window": runs[0].window_start.isoformat() if runs else None,
            "latest_eligible_window": latest_eligible_window(now).isoformat(),
            "missing_registered_windows": missing,
            "finalized_without_snapshot": inconsistent,
            "oldest_unfinished_window": pending[0].window_start.isoformat() if pending else None,
            "nonempty_snapshot_count": sum(row.event_count > 0 for row in windows),
            "snapshot_count": len(windows),
            "continuity_pass": bool(runs) and not missing and not inconsistent and not pending,
            "interpretation": "只验证登记连续性与运行完成状态；空窗口不证明故障识别或根因准确度。",
        }
    path = ROOT / "eval" / f"request-detector-audit-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
