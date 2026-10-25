"""Real broker/Temporal/PG gates with isolated synthetic incidents, not root accuracy."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select
from temporalio.client import Client
from temporalio.service import RPCError

from aegisops.application.contracts import AlertReceivedEvent
from aegisops.application.correlation import correlate_alert
from aegisops.application.incident_corrections import merge_incidents, split_incident
from aegisops.application.incident_workflow import workflow_id_for
from aegisops.application.investigation.service import latest_report
from aegisops.application.remediation.service import PlanRejected, propose_plan
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database import models as m
from aegisops.infrastructure.database.engine import session_for


async def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    tenant = uuid4()
    now = datetime.now(UTC)
    incident_id = None
    refresh_id = None
    child_id = None
    temporal = await Client.connect(settings.temporal_address,
                                    namespace=settings.temporal_namespace)
    gates = {"scope": "synthetic_isolated_incident_refresh_gates_not_fault_accuracy"}

    def add_alert():
        identity = uuid4()
        with session_for(settings.database_url) as db, db.begin():
            db.add(m.AlertRecord(id=identity, tenant_id=tenant, source="refresh-eval",
                external_id=str(identity), source_event_id=str(identity), schema_version=1,
                fingerprint=identity.hex, service_name="refresh-eval", severity="P3",
                status="FIRING", starts_at=now, occurred_at=now, received_at=now,
                payload={"alertname": "RefreshGateHoldout", "resource": "refresh-eval",
                         "signal_type": "availability"}))
        return identity

    async def wait_report(version=None, identity=None):
        for _ in range(100):
            report = latest_report(settings, tenant, identity or incident_id)
            if report and not report.is_stale and (
                version is None or report.source_incident_version == version
            ):
                return report
            await asyncio.sleep(0.3)
        raise AssertionError("automatic current report did not arrive")

    try:
        with session_for(settings.database_url) as db, db.begin():
            db.add(m.TenantRecord(id=tenant, tenant_id=tenant, name=f"refresh-eval-{tenant}"))
        first = add_alert()
        correlate_alert(settings, AlertReceivedEvent(message_id=uuid4(), tenant_id=tenant,
                                                    alert_id=first))
        with session_for(settings.database_url) as db:
            incident_id = db.scalar(select(m.IncidentAlertRecord.incident_id).where(
                m.IncidentAlertRecord.tenant_id == tenant, m.IncidentAlertRecord.alert_id == first))
        baseline = await wait_report()
        second = add_alert()
        event = AlertReceivedEvent(message_id=uuid4(), tenant_id=tenant, alert_id=second)
        assert correlate_alert(settings, event)
        with session_for(settings.database_url) as db:
            version = db.scalar(select(m.IncidentRecord.version).where(
                m.IncidentRecord.tenant_id == tenant, m.IncidentRecord.id == incident_id))
            outbox = db.scalar(select(m.OutboxEventRecord).where(
                m.OutboxEventRecord.tenant_id == tenant,
                m.OutboxEventRecord.event_type == "incident.updated.v1"))
            assert version == baseline.source_incident_version + 1 and outbox is not None
        gates["same_severity_time_membership_increments_version"] = True
        refresh_id = f"incident-refresh:{tenant}:{incident_id}:v{version}"
        refreshed = await wait_report(version)
        assert {e.source_ref for e in refreshed.evidence if e.kind == "NORMALIZED_ALERT"} == {
            f"alert:{first}", f"alert:{second}"}
        assert await temporal.get_workflow_handle(refresh_id).result() == "INCONCLUSIVE"
        assert not correlate_alert(settings, event)
        with session_for(settings.database_url) as db:
            assert db.scalar(select(m.IncidentRecord.version).where(
                m.IncidentRecord.id == incident_id)) == version
        gates["automatic_refresh_contains_new_member"] = True
        gates["duplicate_alert_message_no_version_change"] = True
        # Emulate the old bug: membership changes while the stored version stays identical.
        third = add_alert()
        with session_for(settings.database_url) as db, db.begin():
            db.add(m.IncidentAlertRecord(tenant_id=tenant, incident_id=incident_id, alert_id=third))
            saved = db.get(m.InvestigationReportRecord, refreshed.id)
            saved.status, saved.primary_hypothesis = "SUPPORTED", "redis_unavailable"
        assert latest_report(settings, tenant, incident_id).is_stale
        try:
            propose_plan(settings, tenant, incident_id)
        except PlanRejected as exc:
            assert str(exc) == "investigation report is stale"
        else:
            raise AssertionError("membership-stale report created a plan")
        gates["historical_same_version_membership_mismatch_detected"] = True
        gates["stale_supported_report_rejected_before_plan_creation"] = True
        # Re-enter through a real membership change, then exercise operator corrections.
        fourth = add_alert()
        correlate_alert(settings, AlertReceivedEvent(message_id=uuid4(), tenant_id=tenant,
                                                    alert_id=fourth))
        before_split = await wait_report()
        scoped = settings.model_copy(update={"dev_tenant_id": tenant})
        child_id, moved = split_incident(
            scoped, incident_id, expected_version=before_split.current_incident_version,
            alert_ids=[first], title="isolated split refresh gate",
            reason="隔离门禁验证拆分后的来源报告刷新", operator_id="isolated-evaluator")
        assert moved == 1
        source_report = await wait_report(before_split.current_incident_version + 1)
        child_report = await wait_report(identity=child_id)
        assert {e.source_ref for e in source_report.evidence if e.kind == "NORMALIZED_ALERT"} == {
            f"alert:{second}", f"alert:{third}", f"alert:{fourth}"}
        assert {e.source_ref for e in child_report.evidence if e.kind == "NORMALIZED_ALERT"} == {
            f"alert:{first}"}
        gates["split_refreshes_source_and_new_incident_membership"] = True
        merge_incidents(scoped, child_id, incident_id,
                        source_version=child_report.current_incident_version,
                        target_version=source_report.current_incident_version,
                        reason="隔离门禁验证合并后的目标报告刷新", operator_id="isolated-evaluator")
        merged = await wait_report(source_report.current_incident_version + 1)
        assert {e.source_ref for e in merged.evidence if e.kind == "NORMALIZED_ALERT"} == {
            f"alert:{first}", f"alert:{second}", f"alert:{third}", f"alert:{fourth}"}
        gates["merge_refreshes_target_membership"] = True
    finally:
        cleanup_ids = [workflow_id_for(tenant, incident_id), refresh_id] if incident_id else []
        if child_id:
            cleanup_ids.append(workflow_id_for(tenant, child_id))
        for identity in cleanup_ids:
            if identity:
                try:
                    await temporal.get_workflow_handle(identity).terminate("isolated gate cleanup")
                except RPCError:
                    pass
        with session_for(settings.database_url) as db, db.begin():
            for cls in (m.InvestigationEvidenceRecord, m.InvestigationReportRecord,
                        m.IncidentWorkflowStartRecord, m.IncidentTimelineRecord,
                        m.CorrelationDecisionRecord, m.InboxMessageRecord, m.OutboxEventRecord,
                        m.IncidentRelationRecord, m.IncidentAlertRecord,
                        m.IncidentRecord, m.AlertRecord, m.TenantRecord):
                db.execute(delete(cls).where(cls.tenant_id == tenant))
        gates["isolated_data_cleaned"] = True
    Path("/tmp/incident-refresh-gates.json").write_text(
        json.dumps(gates, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(gates, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
