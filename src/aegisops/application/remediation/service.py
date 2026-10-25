"""Turn a supported current report into one immutable, unexecuted action proposal."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from aegisops.application.contracts import DeploymentTransition
from aegisops.application.investigation.service import latest_report
from aegisops.application.investigation.signal_catalog import load_signal_catalog
from aegisops.config import Settings
from aegisops.domain.models import ActionSnapshot, IncidentStatus
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionRecord,
    AgentRunRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    RemediationPlanRecord,
    ResourceRecord,
    ServiceRecord,
)

from .agent_mapping import agent_action_candidate
from .catalog import ActionTemplate, RemediationCatalog, load_remediation_catalog


class PlanRejected(ValueError):
    pass


class ProposedActionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str
    snapshot: ActionSnapshot
    snapshot_hash: str


class RemediationPlanView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    incident_id: UUID
    source_report_id: UUID
    source_incident_version: int
    current_incident_version: int
    is_stale: bool
    status: str
    rationale: str
    root_cause_summary: str
    risk_summary: str
    blast_radius: str
    plan_hash: str
    catalog_version: str
    actions: list[ProposedActionView]


def _canonical_hash(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _rollback_parameters(payload: dict | None) -> dict[str, str | bool]:
    if payload is None:
        raise PlanRejected("rollback requires a recorded deployment change")
    value = payload.get("from_ref")
    if not isinstance(value, str) or not value:
        raise PlanRejected("rollback requires a recorded prior release reference")
    try:
        deployment = DeploymentTransition.model_validate(payload.get("deployment"))
    except ValidationError as exc:
        raise PlanRejected("rollback requires recorded deployment fingerprints") from exc
    if (
        deployment.before.change_id != value
        or deployment.after.change_id != payload.get("to_ref")
        or deployment.before.bad_deployment
        or not deployment.after.bad_deployment
    ):
        raise PlanRejected("deployment fingerprints do not match the change event")
    return {
        "to_ref": value,
        "target_image_id": deployment.before.image_id,
        "target_config_hash": deployment.before.compose_config_hash,
        "current_image_id": deployment.after.image_id,
        "current_config_hash": deployment.after.compose_config_hash,
        "current_change_id": deployment.after.change_id,
        "target_bad_deployment": deployment.before.bad_deployment,
        "current_bad_deployment": deployment.after.bad_deployment,
    }


def _target(session, tenant_id: UUID, template: ActionTemplate) -> tuple[str, int]:
    if template.target_kind == "resource":
        row = session.scalar(
            select(ResourceRecord).where(
                ResourceRecord.tenant_id == tenant_id,
                ResourceRecord.environment == "lab",
                ResourceRecord.slug == template.target_slug,
            )
        )
    else:
        row = session.scalar(
            select(ServiceRecord).where(
                ServiceRecord.tenant_id == tenant_id,
                ServiceRecord.environment == "lab",
                ServiceRecord.slug == template.target_slug,
            )
        )
    if row is None:
        raise PlanRejected(f"target {template.target_kind}/{template.target_slug} is absent")
    return f"{template.target_kind}/{row.id}", row.version


def get_plan(settings: Settings, tenant_id: UUID, incident_id: UUID) -> RemediationPlanView | None:
    with session_for(settings.database_url) as session:
        row = session.scalar(
            select(RemediationPlanRecord)
            .where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.incident_id == incident_id,
                RemediationPlanRecord.source_report_id.is_not(None),
            )
            .order_by(RemediationPlanRecord.created_at.desc(), RemediationPlanRecord.id.desc())
            .limit(1)
        )
        if row is None or row.source_report_id is None or row.source_incident_version is None:
            return None
        if row.plan_hash is None or row.catalog_version is None:
            return None
        incident_version = session.scalar(
            select(IncidentRecord.version).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if incident_version is None:
            return None
        actions = session.scalars(
            select(ActionRecord)
            .where(ActionRecord.tenant_id == tenant_id, ActionRecord.plan_id == row.id)
            .order_by(ActionRecord.created_at, ActionRecord.id)
        ).all()
        return RemediationPlanView(
            id=row.id,
            incident_id=incident_id,
            source_report_id=row.source_report_id,
            source_incident_version=row.source_incident_version,
            current_incident_version=incident_version,
            is_stale=incident_version != row.source_incident_version,
            status=row.status,
            rationale=row.rationale,
            root_cause_summary=row.root_cause_summary or "unavailable",
            risk_summary=row.risk_summary or "unavailable",
            blast_radius=row.blast_radius or "unavailable",
            plan_hash=row.plan_hash,
            catalog_version=row.catalog_version,
            actions=[
                ProposedActionView(
                    id=item.id,
                    status=item.status,
                    snapshot=ActionSnapshot.model_validate(item.snapshot),
                    snapshot_hash=ActionSnapshot.model_validate(item.snapshot).digest(),
                )
                for item in actions
            ],
        )


def propose_plan(
    settings: Settings,
    tenant_id: UUID,
    incident_id: UUID,
    catalog: RemediationCatalog | None = None,
) -> RemediationPlanView:
    report = latest_report(settings, tenant_id, incident_id)
    if report is None or report.is_stale:
        raise PlanRejected("a current investigation report is required")
    catalog = catalog or load_remediation_catalog()
    timeline_details: dict = {}
    if report.graph_version.startswith("readonly-agent-"):
        # Agent reports stay INCONCLUSIVE by design; a candidate reaches the
        # catalog only through the verified-claim gate plus the reviewed mapping.
        if report.status != "INCONCLUSIVE" or report.primary_hypothesis is not None:
            raise PlanRejected("an autonomous report must stay inconclusive")
        candidate = agent_action_candidate(report, catalog)
        if candidate is None:
            raise PlanRejected(
                "a gate-supported candidate with a mapped governed action is required"
            )
        check, gate, template = candidate
        assert check.claim is not None
        parameters: dict[str, str | int | float | bool | None] = {}
        rationale = (
            f"自主调查候选{check.candidate_index}通过机制与来源门禁：已核验 "
            f"{check.claim.kind} {check.claim.service}→{check.claim.dependency} "
            f"{check.claim.failure_code or check.claim.state}"
            f"（直接来源覆盖 {check.verified_member_count}/{check.source_member_count}）；"
            "当前故障状态未知，执行前按既有前置条件复读，不可跳过审批。"
        )
        timeline_details.update({
            "agent_candidate_index": check.candidate_index,
            "verified_claim": check.claim.model_dump(mode="json"),
            "root_cause_gate": gate.model_dump(mode="json"),
            "verified_evidence_ids": [str(e) for e in check.supporting_evidence_ids],
        })
    else:
        if report.status != "SUPPORTED" or report.primary_hypothesis is None:
            raise PlanRejected("a supported investigation report is required")
        current_signal = next(
            (
                signal for signal in load_signal_catalog().signals
                if signal.hypothesis_key == report.primary_hypothesis and signal.root_candidate
            ),
            None,
        )
        if current_signal is None or not any(
            item.source_type == "METRIC"
            and item.polarity == "SUPPORT"
            and current_signal.metric_name in item.source_ref
            for item in report.evidence
        ):
            raise PlanRejected("current signal metric evidence is required")
        found = catalog.find(report.primary_hypothesis)
        if found is None:
            raise PlanRejected("no reviewed remediation action for this hypothesis")
        template = found
        parameters = {}
        if template.parameter_from_change:
            change = next(
                (
                    item
                    for item in report.evidence
                    if item.source_type == "CHANGE"
                    and item.subject == report.primary_hypothesis
                    and item.polarity == "SUPPORT"
                ),
                None,
            )
            parameters.update(_rollback_parameters(change.payload if change else None))
        rationale = template.rationale
    with session_for(settings.database_url) as session, session.begin():
        incident = session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id)
            .with_for_update()
        )
        if incident is None or incident.status not in {
            IncidentStatus.OPEN,
            IncidentStatus.MITIGATING,
        }:
            raise PlanRejected("incident is not active")
        existing = session.scalar(
            select(RemediationPlanRecord.id).where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.source_report_id == report.id,
            )
        )
        if existing is not None:
            pass
        elif incident.version != report.source_incident_version:
            raise PlanRejected("investigation report is stale")
        else:
            target, resource_version = _target(session, tenant_id, template)
            plan_hash = _canonical_hash(
                {
                    "report_id": str(report.id),
                    "incident_version": incident.version,
                    "catalog_version": catalog.version,
                    "action_type": template.action_type,
                    "target": target,
                    "resource_version": resource_version,
                    "parameters": parameters,
                    "risk": template.risk,
                }
            )
            snapshot = ActionSnapshot(
                action_type=template.action_type,
                target=target,
                parameters=parameters,
                resource_version=resource_version,
                plan_hash=plan_hash,
                risk=template.risk,
            )
            run_id, plan_id, action_id = uuid4(), uuid4(), uuid4()
            session.add(
                AgentRunRecord(
                    id=run_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    thread_id=run_id,
                    status="COMPLETED",
                    graph_version=report.graph_version,
                    prompt_version="none",
                    model_provider="rules",
                    model_name=catalog.version,
                    max_llm_calls=0,
                    max_tool_calls=0,
                )
            )
            session.flush()
            session.add(
                RemediationPlanRecord(
                    id=plan_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    agent_run_id=run_id,
                status="PROPOSED",
                rationale=rationale,
                source_report_id=report.id,
                    source_incident_version=incident.version,
                    plan_hash=plan_hash,
                    catalog_version=catalog.version,
                    root_cause_summary=report.summary,
                    risk_summary=(
                        "高风险：执行前必须经过独立人工审批。"
                    ),
                    blast_radius=template.blast_radius,
                )
            )
            session.flush()
            session.add(
                ActionRecord(
                    id=action_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    plan_id=plan_id,
                    status="PROPOSED",
                    snapshot=snapshot.model_dump(mode="json"),
                )
            )
            session.add(
                IncidentTimelineRecord(
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    event_type="REMEDIATION_PROPOSED",
                    summary=f"Proposed {template.action_type} for operator review",
                    details={
                        "report_id": str(report.id),
                        "plan_id": str(plan_id),
                        "action_id": str(action_id),
                        "plan_hash": plan_hash,
                        "risk": template.risk,
                        "snapshot_hash": snapshot.digest(),
                        "created_at": datetime.now(UTC).isoformat(),
                        **timeline_details,
                    },
                )
            )
    proposal = get_plan(settings, tenant_id, incident_id)
    if proposal is None or proposal.source_report_id != report.id:
        raise RuntimeError("proposed plan was not persisted")
    return proposal
