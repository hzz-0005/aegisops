"""Promote persisted real signals; audit provenance and unknown-root execution denial."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select

from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord as Alert,
)
from aegisops.infrastructure.database.models import (
    InvestigationEvidenceRecord as Evidence,
)
from aegisops.infrastructure.database.models import (
    InvestigationReportRecord as Report,
)
from aegisops.infrastructure.database.models import (
    OutboxEventRecord as Outbox,
)
from aegisops.infrastructure.database.models import (
    RemediationPlanRecord as Plan,
)
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalEventRecord as Member,
)
from aegisops.infrastructure.database.models import (
    RequestDerivedSignalRecord as Signal,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-start", required=True)
    args = parser.parse_args()
    start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
    settings = Settings()
    require_dev_runtime(settings)
    with session_for(settings.database_url) as session:
        signals = session.scalars(
            select(Signal).where(
                Signal.tenant_id == settings.dev_tenant_id,
                Signal.window_start == start,
            )
        ).all()
        truth = [(row.id, row.event_count) for row in signals]
    assert truth, "no persisted signals in requested window"
    results = []
    headers = {"Authorization": f"Bearer {settings.operator_token or 'aegisops_operator_dev_only'}"}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=20) as client:
        for signal_id, count in truth:
            path = f"/api/v1/request-failure-signals/{signal_id}/promote"
            unauthorized = client.post(path, json={"reason": "实验核对直接来源"})
            assert unauthorized.status_code == 401
            response = client.post(
                path, headers=headers, json={"reason": "核对真实实验来源与症状边界"}
            )
            response.raise_for_status()
            alert_id = response.json()["alert_id"]
            duplicate = client.post(path, headers=headers, json={"reason": "验证重复提交幂等性"})
            duplicate.raise_for_status()
            assert duplicate.json()["alert_id"] == alert_id
            deadline = time.monotonic() + 55
            matched = None
            while time.monotonic() < deadline:
                with session_for(settings.database_url) as session:
                    rows = session.execute(
                        select(Evidence, Report)
                        .join(
                            Report,
                            (Evidence.report_id == Report.id)
                            & (Evidence.tenant_id == Report.tenant_id),
                        )
                        .where(
                            Evidence.tenant_id == settings.dev_tenant_id,
                            Evidence.kind == "REQUEST_SIGNAL_MEMBERSHIP",
                        )
                    ).all()
                    for evidence, report in rows:
                        if evidence.payload.get("alert_id") == alert_id:
                            matched = (
                                dict(evidence.payload),
                                report.incident_id,
                                report.status,
                                report.primary_hypothesis,
                                evidence.polarity,
                            )
                if matched:
                    break
                time.sleep(2)
            assert matched is not None, "no persisted investigation membership"
            payload, incident_id, status, root, polarity = matched
            assert status == "INCONCLUSIVE" and root is None and polarity == "CONTEXT"
            assert payload["alert_binding_status"] == "DIRECT_SOURCE_MEMBERSHIP"
            assert payload["current_fault_status"] == "UNKNOWN"
            with session_for(settings.database_url) as session:
                member_ids = set(
                    str(value)
                    for value in session.scalars(
                        select(Member.request_event_id).where(
                            Member.tenant_id == settings.dev_tenant_id,
                            Member.signal_id == signal_id,
                        )
                    )
                )
                assert set(payload["member_event_ids"]) == member_ids and len(member_ids) == count
                outboxes = session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(
                        Outbox.tenant_id == settings.dev_tenant_id,
                        Outbox.event_type == "alert.received.v1",
                        Outbox.payload["alert_id"].astext == alert_id,
                    )
                )
                assert outboxes == 1
                plans = session.scalar(
                    select(func.count())
                    .select_from(Plan)
                    .where(
                        Plan.tenant_id == settings.dev_tenant_id,
                        Plan.incident_id == incident_id,
                    )
                )
                assert plans == 0
                alert = session.scalar(
                    select(Alert).where(
                        Alert.tenant_id == settings.dev_tenant_id, Alert.id == alert_id
                    )
                )
                assert alert is not None and alert.status == "OBSERVED"
            denied = client.post(
                f"/api/v1/incidents/{incident_id}/remediation-plan", headers=headers
            )
            assert denied.status_code == 409, denied.text
            results.append(
                {
                    "signal_id": str(signal_id),
                    "alert_id": alert_id,
                    "incident_id": str(incident_id),
                    "exact_member_count": count,
                    "report_status": status,
                    "duplicate_same_alert": True,
                    "outbox_count": outboxes,
                    "unknown_root_plan_denied": True,
                }
            )
    now = datetime.now(UTC)
    report_path = Path("eval") / f"source-membership-{now:%Y%m%dT%H%M%SZ}.json"
    report_path.write_text(
        json.dumps(
            {
                "captured_at": now.isoformat(),
                "window_start": start.isoformat(),
                "passed": True,
                "results": results,
                "scope": "历史真实来源回放，不是新增故障样本；来源绑定不证明根因或当前故障。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"passed": True, "signals": len(results), "report": str(report_path)}))


if __name__ == "__main__":
    main()
