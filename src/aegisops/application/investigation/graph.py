"""LangGraph stays inside one read-only investigation activity."""

from datetime import UTC, datetime, timedelta
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

import httpx
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from aegisops.config import Settings
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    ChangeEventRecord,
    IncidentAlertRecord,
    IncidentRecord,
    ResourceRecord,
    ServiceDependencyRecord,
    ServiceRecord,
)

from .contracts import Evidence, Hypothesis, InvestigationReport
from .read_tools import ReadOnlyTelemetryTools
from .signal_catalog import SignalCatalog, SignalDefinition, load_signal_catalog

GRAPH_VERSION = "readonly-investigation-v10-database-ancestry"


class AlertFact(TypedDict):
    id: UUID
    source_event_id: str
    alert_name: str
    service: str
    resource: str | None
    change_id: str | None
    occurred_at: datetime
    status: str


class IncidentSnapshot(TypedDict):
    id: UUID
    tenant_id: UUID
    version: int
    title: str
    alerts: list[AlertFact]
    changes: dict[str, dict]
    service_ids: dict[str, str]
    dependencies: dict[str, list[str]]
    recent_changes: NotRequired[dict[str, list[dict]]]


class InvestigationState(TypedDict):
    tenant_id: UUID
    incident_id: UUID
    snapshot: NotRequired[IncidentSnapshot]
    evidence: NotRequired[list[Evidence]]
    hypotheses: NotRequired[list[Hypothesis]]
    limitations: NotRequired[list[str]]
    outcome: NotRequired[str]
    report: NotRequired[InvestigationReport]
    agent_done: NotRequired[bool]
    agent_retry: NotRequired[bool]


def load_snapshot(settings: Settings, tenant_id: UUID, incident_id: UUID) -> IncidentSnapshot:
    with session_for(settings.database_url) as session:
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == incident_id
            )
        )
        if incident is None:
            raise LookupError("incident not found")
        alerts = session.scalars(
            select(AlertRecord)
            .join(
                IncidentAlertRecord,
                (IncidentAlertRecord.tenant_id == AlertRecord.tenant_id)
                & (IncidentAlertRecord.alert_id == AlertRecord.id),
            )
            .where(
                IncidentAlertRecord.tenant_id == tenant_id,
                IncidentAlertRecord.incident_id == incident_id,
            )
            .order_by(AlertRecord.occurred_at, AlertRecord.id)
        ).all()
        alert_facts: list[AlertFact] = [
            {
                "id": alert.id,
                "source_event_id": alert.source_event_id,
                "alert_name": alert.payload["alertname"],
                "service": alert.service_name,
                "resource": alert.payload.get("resource"),
                "change_id": alert.payload.get("change_id"),
                "occurred_at": alert.occurred_at,
                "status": alert.status,
            }
            for alert in alerts
        ]
        change_ids = {alert["change_id"] for alert in alert_facts if alert["change_id"]}
        changes = {
            row.external_id: {
                "id": str(row.id),
                "external_id": row.external_id,
                "change_type": row.change_type,
                "occurred_at": row.occurred_at,
                "service_id": str(row.service_id),
                "from_ref": row.from_ref,
                "to_ref": row.to_ref,
                "deployment": row.attributes,
            }
            for row in session.scalars(
                select(ChangeEventRecord).where(
                    ChangeEventRecord.tenant_id == tenant_id,
                    ChangeEventRecord.external_id.in_(change_ids),
                )
            ).all()
        }
        services = session.scalars(
            select(ServiceRecord).where(
                ServiceRecord.tenant_id == tenant_id,
                ServiceRecord.slug.in_({alert["service"] for alert in alert_facts}),
            )
        ).all()
        recent_changes: dict[str, list[dict]] = {}
        if services and alert_facts:
            earliest = min(alert["occurred_at"] for alert in alert_facts) - timedelta(minutes=30)
            latest = max(alert["occurred_at"] for alert in alert_facts)
            service_names = {service.id: service.slug for service in services}
            recent_change_rows = session.scalars(
                select(ChangeEventRecord)
                .where(
                    ChangeEventRecord.tenant_id == tenant_id,
                    ChangeEventRecord.service_id.in_(service_names),
                    ChangeEventRecord.occurred_at >= earliest,
                    ChangeEventRecord.occurred_at <= latest,
                )
                .order_by(ChangeEventRecord.occurred_at.desc())
                .limit(20)
            ).all()
            for change_row in recent_change_rows:
                recent_changes.setdefault(service_names[change_row.service_id], []).append(
                    {
                        "id": str(change_row.id),
                        "external_id": change_row.external_id,
                        "change_type": change_row.change_type,
                        "occurred_at": change_row.occurred_at,
                    }
                )
        dependencies: dict[str, list[str]] = {}
        for service in services:
            rows = session.scalars(
                select(ServiceDependencyRecord).where(
                    ServiceDependencyRecord.tenant_id == tenant_id,
                    ServiceDependencyRecord.service_id == service.id,
                )
            ).all()
            resources = session.scalars(
                select(ResourceRecord).where(
                    ResourceRecord.tenant_id == tenant_id,
                    ResourceRecord.id.in_(
                        [row.depends_on_resource_id for row in rows if row.depends_on_resource_id]
                    ),
                )
            ).all()
            names = [resource.slug for resource in resources]
            service_ids = [row.depends_on_service_id for row in rows if row.depends_on_service_id]
            if service_ids:
                names.extend(
                    session.scalars(
                        select(ServiceRecord.slug).where(
                            ServiceRecord.tenant_id == tenant_id,
                            ServiceRecord.id.in_(service_ids),
                        )
                    ).all()
                )
            dependencies[service.slug] = sorted(names)
        return {
            "id": incident.id,
            "tenant_id": incident.tenant_id,
            "version": incident.version,
            "title": incident.title,
            "alerts": alert_facts,
            "changes": changes,
            "service_ids": {service.slug: str(service.id) for service in services},
            "dependencies": dependencies,
            "recent_changes": recent_changes,
        }


def build_investigation_graph(settings: Settings, catalog: SignalCatalog | None = None):
    catalog = catalog or load_signal_catalog()

    def triage(state: InvestigationState) -> dict:
        return {
            "snapshot": load_snapshot(settings, state["tenant_id"], state["incident_id"]),
            "limitations": [],
        }

    def collect(state: InvestigationState) -> dict:
        snapshot = state["snapshot"]
        evidence: list[Evidence] = []
        limitations: list[str] = []
        with ReadOnlyTelemetryTools(settings) as tools:
            for alert in snapshot["alerts"]:
                signal = catalog.find(alert["alert_name"], alert["service"])
                subject = signal.hypothesis_key if signal else "unclassified"
                evidence.append(
                    Evidence(
                        source_type="ALERT",
                        source_ref=f"alert:{alert['id']}",
                        kind="NORMALIZED_ALERT",
                        subject=subject,
                        polarity="SUPPORT",
                        observed_at=alert["occurred_at"],
                        collected_at=datetime.now(UTC),
                        tool_name="incident.alerts",
                        tool_version="normalized-alert-v1",
                        payload={
                            "alert_name": alert["alert_name"],
                            "service": alert["service"],
                            "resource": alert["resource"],
                            "change_id": alert["change_id"],
                            "source_event_id": alert["source_event_id"],
                            "status": alert["status"],
                        },
                    )
                )
                dependencies = snapshot["dependencies"].get(alert["service"], [])
                for context_name, context_call in (
                    ("incoming request facts", lambda: tools.generic_incoming_requests(
                        tenant_id=state["tenant_id"], service=alert["service"],
                        occurred_at=alert["occurred_at"],
                    )),
                    ("database operation facts", lambda: tools.generic_database_requests(
                        tenant_id=state["tenant_id"], services=[alert["service"], *dependencies],
                        occurred_at=alert["occurred_at"],
                    )),
                    ("request source membership", lambda: tools.request_alert_membership(
                        tenant_id=state["tenant_id"], alert_id=alert["id"],
                    )),
                    ("request ancestry", lambda: tools.generic_request_links(
                        tenant_id=state["tenant_id"], service=alert["service"],
                        occurred_at=alert["occurred_at"],
                    )),
                    ("runtime observations", lambda: tools.generic_runtime_state(
                        services=[alert["service"], *dependencies],
                        occurred_at=alert["occurred_at"],
                    )),
                    ("dependency request facts", lambda: tools.generic_dependency_requests(
                        tenant_id=state["tenant_id"], service=alert["service"],
                        dependencies=dependencies, occurred_at=alert["occurred_at"],
                    )),
                ):
                    try:
                        evidence.extend(context_call())
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        limitations.append(f"{context_name} unavailable: {type(exc).__name__}")
                if signal is None:
                    limitations.append(
                        f"no signal definition for {alert['alert_name']} on {alert['service']}"
                    )
                    if alert["service"] not in snapshot["dependencies"]:
                        limitations.append(f"service {alert['service']} is absent from catalog")
                    else:
                        evidence.append(
                            Evidence(
                                source_type="CATALOG",
                                source_ref=f"catalog:service:{alert['service']}",
                                kind="SERVICE_DEPENDENCIES",
                                subject="unclassified",
                                polarity="CONTEXT",
                                observed_at=None,
                                collected_at=datetime.now(UTC),
                                tool_name="catalog.read",
                                tool_version="catalog-v1",
                                payload={"service": alert["service"], "dependencies": dependencies},
                            )
                        )
                    for change in snapshot.get("recent_changes", {}).get(alert["service"], []):
                        age = (alert["occurred_at"] - change["occurred_at"]).total_seconds()
                        if not 0 <= age <= 1800:
                            continue
                        evidence.append(
                            Evidence(
                                source_type="CHANGE",
                                source_ref=f"change:{change['id']}",
                                kind="RECENT_SERVICE_CHANGE",
                                subject="unclassified",
                                polarity="CONTEXT",
                                observed_at=change["occurred_at"],
                                collected_at=datetime.now(UTC),
                                tool_name="change_events.read",
                                tool_version="change-event-v1",
                                payload={
                                    "service": alert["service"],
                                    "change_type": change["change_type"],
                                },
                            )
                        )
                    try:
                        metric = tools.generic_http_activity(
                            service=alert["service"], occurred_at=alert["occurred_at"]
                        )
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        limitations.append(
                            f"prometheus.query_range unavailable: {type(exc).__name__}"
                        )
                    else:
                        if metric is None:
                            limitations.append(f"no generic HTTP samples for {alert['service']}")
                        else:
                            evidence.append(metric)
                    for generic_tool_name, generic_call in (
                        ("loki.query_range", tools.generic_error_logs),
                        ("tempo.search_and_get_trace", tools.generic_error_traces),
                    ):
                        try:
                            evidence.extend(
                                generic_call(
                                    service=alert["service"], occurred_at=alert["occurred_at"]
                                )
                            )
                        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                            limitations.append(
                                f"{generic_tool_name} unavailable: {type(exc).__name__}"
                            )
                    try:
                        evidence.extend(tools.generic_probe_health(
                            services=[alert["service"], *(dependencies or [])],
                            occurred_at=alert["occurred_at"],
                        ))
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        limitations.append(f"dependency probes unavailable: {type(exc).__name__}")
                    continue
                evidence.extend(_context_evidence(snapshot, alert, signal, limitations))
                for tool_name, call in (
                    (
                        "prometheus.query_range",
                        lambda: tools.metric(
                            signal,
                            service=alert["service"],
                            change_id=alert["change_id"],
                            occurred_at=alert["occurred_at"],
                        ),
                    ),
                    (
                        "loki.query_range",
                        lambda: tools.logs(
                            signal, service=alert["service"], occurred_at=alert["occurred_at"]
                        ),
                    ),
                    (
                        "tempo.search_and_get_trace",
                        lambda: tools.traces(
                            signal, service=alert["service"], occurred_at=alert["occurred_at"]
                        ),
                    ),
                ):
                    try:
                        result = call()
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        limitations.append(f"{tool_name} unavailable: {type(exc).__name__}")
                        continue
                    if result is None:
                        if tool_name == "prometheus.query_range":
                            limitations.append(f"no metric samples for {alert['alert_name']}")
                    elif isinstance(result, list):
                        evidence.extend(result)
                    else:
                        evidence.append(result)
        unique: dict[tuple[str, str, str, str], Evidence] = {}
        for item in evidence:
            unique.setdefault((item.source_type, item.source_ref, item.kind, item.subject), item)
        return {"evidence": list(unique.values()), "limitations": sorted(set(limitations))}

    def hypothesize(state: InvestigationState) -> dict:
        snapshot = state["snapshot"]
        evidence = state["evidence"]
        signals = {
            signal.hypothesis_key: signal
            for alert in snapshot["alerts"]
            if (signal := catalog.find(alert["alert_name"], alert["service"])) is not None
            and signal.root_candidate
        }
        hypotheses: list[Hypothesis] = []
        weights = {"ALERT": 0.25, "METRIC": 0.35, "LOG": 0.20, "TRACE": 0.10, "CHANGE": 0.30}
        for key, signal in signals.items():
            supports = [
                item for item in evidence if item.subject == key and item.polarity == "SUPPORT"
            ]
            conflicts = [
                item for item in evidence if item.subject == key and item.polarity == "CONFLICT"
            ]
            source_types = {item.source_type for item in supports}
            score = sum(weights.get(source_type, 0) for source_type in source_types)
            score -= 0.2 * len({item.source_type for item in conflicts})
            score = round(max(0.0, min(1.0, score)), 3)
            hypotheses.append(
                Hypothesis(
                    key=key,
                    label=signal.hypothesis_label,
                    score=score,
                    supporting_evidence_ids=[item.id for item in supports],
                    conflicting_evidence_ids=[item.id for item in conflicts],
                    rationale=(
                        f"支持证据来源：{', '.join(sorted(source_types)) or '无'}；"
                        f"冲突证据来源："
                        f"{', '.join(sorted({item.source_type for item in conflicts})) or '无'}。"
                    ),
                )
            )
        hypotheses.sort(key=lambda item: (-item.score, item.key))
        return {"hypotheses": hypotheses}

    def verify(state: InvestigationState) -> dict:
        hypotheses = state["hypotheses"]
        if not hypotheses:
            return {"outcome": "INCONCLUSIVE"}
        top = hypotheses[0]
        signal = next((item for item in catalog.signals if item.hypothesis_key == top.key), None)
        supports = [item for item in state["evidence"] if item.id in top.supporting_evidence_ids]
        distinct_sources = {item.source_type for item in supports}
        margin = top.score - (hypotheses[1].score if len(hypotheses) > 1 else 0)
        change_verified = signal is not None and (
            not signal.requires_change or "CHANGE" in distinct_sources
        )
        if top.score >= 0.6 and "METRIC" in distinct_sources and change_verified and margin > 0.1:
            return {"outcome": "SUPPORTED"}
        return {"outcome": "INCONCLUSIVE"}

    def finish(state: InvestigationState) -> dict:
        top = state["hypotheses"][0] if state["hypotheses"] else None
        status: Literal["SUPPORTED", "INCONCLUSIVE"] = (
            "SUPPORTED" if state["outcome"] == "SUPPORTED" else "INCONCLUSIVE"
        )
        if status == "SUPPORTED" and top is not None:
            summary = f"可能根因：{top.label}；执行任何操作前请核对关联证据。"
        elif any(
            item.subject == "container_oom_event"
            and item.kind == "COUNTER_INCREASE"
            and item.polarity == "SUPPORT"
            for item in state["evidence"]
        ):
            summary = "已观测到容器 OOM 进程终止；内存增长来源及业务影响仍需调查。"
        elif any(item.kind == "RUNTIME_STATE_OBSERVATION" and item.payload.get("state") == "paused"
                 for item in state["evidence"]):
            summary = "事故采样窗口观测到业务容器暂停；是否解释全部业务失败仍需核对请求证据。"
        else:
            summary = "现有证据不足或存在冲突，需要运维人员继续调查。"
        report = InvestigationReport(
            tenant_id=state["tenant_id"],
            incident_id=state["incident_id"],
            source_incident_version=state["snapshot"]["version"],
            status=status,
            graph_version=GRAPH_VERSION,
            summary=summary,
            primary_hypothesis=top.key if top else None,
            confidence=top.score if top else 0,
            hypotheses=state["hypotheses"],
            evidence=state["evidence"],
            limitations=state["limitations"],
            completed_at=datetime.now(UTC),
        )
        return {"report": report}

    graph = StateGraph(InvestigationState)
    graph.add_node("triage", triage)
    if settings.investigation_agent_enabled:
        from .agent_loop import AgentLoop

        agent = AgentLoop(settings)
        graph.add_node("agent_prepare", agent.prepare)
        graph.add_node("agent_plan", agent.plan)
        graph.add_node("agent_observe", agent.observe)
        graph.add_node("agent_report", agent.finish)
        graph.add_edge(START, "triage")
        graph.add_edge("triage", "agent_prepare")
        graph.add_conditional_edges("agent_prepare",
                                    lambda s: "agent_report" if s["agent_done"] else "agent_plan")
        graph.add_conditional_edges(
            "agent_plan",
            lambda s: ("agent_report" if s["agent_done"]
                       else "agent_plan" if s.get("agent_retry") else "agent_observe"),
        )
        graph.add_conditional_edges("agent_observe",
                                    lambda s: "agent_report" if s["agent_done"] else "agent_plan")
        graph.add_edge("agent_report", END)
        return graph.compile()
    graph.add_node("collect", collect)
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("verify", verify)
    graph.add_node("report", finish)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "collect")
    graph.add_edge("collect", "hypothesize")
    graph.add_edge("hypothesize", "verify")
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph.compile()


def _context_evidence(
    snapshot: IncidentSnapshot,
    alert: AlertFact,
    signal: SignalDefinition,
    limitations: list[str],
) -> list[Evidence]:
    evidence: list[Evidence] = []
    dependencies = snapshot["dependencies"].get(alert["service"])
    if dependencies is None:
        limitations.append(f"service {alert['service']} is absent from catalog")
    else:
        evidence.append(
            Evidence(
                source_type="CATALOG",
                source_ref=f"catalog:service:{alert['service']}",
                kind="SERVICE_DEPENDENCIES",
                subject=signal.hypothesis_key,
                polarity="CONTEXT",
                observed_at=None,
                collected_at=datetime.now(UTC),
                tool_name="catalog.read",
                tool_version="catalog-v1",
                payload={"service": alert["service"], "dependencies": dependencies},
            )
        )
    if signal.requires_change:
        change_id = alert["change_id"]
        change = snapshot["changes"].get(change_id) if change_id else None
        if change is None:
            limitations.append(f"change event {change_id or 'missing'} not found")
        elif change["service_id"] != snapshot["service_ids"].get(alert["service"]):
            limitations.append(f"change event {change_id} belongs to another service")
        elif change["change_type"] != "DEPLOYMENT":
            limitations.append(f"change event {change_id} is not a deployment")
        elif not 0 <= (alert["occurred_at"] - change["occurred_at"]).total_seconds() <= 1800:
            limitations.append(f"change event {change_id} is outside the 30-minute window")
        else:
            evidence.append(
                Evidence(
                    source_type="CHANGE",
                    source_ref=f"change:{change['id']}",
                    kind="DEPLOYMENT_CHANGE",
                    subject=signal.hypothesis_key,
                    polarity="SUPPORT",
                    observed_at=change["occurred_at"],
                    collected_at=datetime.now(UTC),
                    tool_name="change_events.read",
                    tool_version="change-event-v1",
                    payload={
                        "external_id": change["external_id"],
                        "change_type": change["change_type"],
                        "from_ref": change["from_ref"],
                        "to_ref": change["to_ref"],
                        "deployment": change.get("deployment", {}),
                    },
                )
            )
    return evidence
