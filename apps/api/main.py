from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from aegisops.application.alerts import ingest_alerts
from aegisops.application.catalog import ChangeConflict, UnknownService, record_change
from aegisops.application.contracts import (
    AlertmanagerWebhook,
    ChangeEventInput,
    ChangeEventView,
    CorrelationDecisionView,
    IncidentCorrectionResult,
    IncidentRelationView,
    IncidentSummary,
    IncidentWorkflowView,
    IngestResult,
    MergeIncidentsInput,
    OperatorDecision,
    OperatorWorkflowDecision,
    ReconcileWorkflowStartInput,
    SplitIncidentInput,
    TimelineItem,
)
from aegisops.application.incident_corrections import (
    IncidentCorrectionRejected,
    merge_incidents,
    split_incident,
)
from aegisops.application.incident_lifecycle import (
    IncidentNotFound,
    IncidentVersionConflict,
    change_incident_status,
    escalate_incident_phase,
)
from aegisops.application.incident_request_evidence import (
    AttachRequestEvidenceInput,
    IncidentRequestEvidenceView,
    RequestEvidenceConflict,
    RequestEvidenceNotFound,
    RequestEvidenceNotVerified,
    attach_request_evidence,
    list_request_evidence,
)
from aegisops.application.incident_workflow import (
    reconcile_existing_workflow_start,
    workflow_id_for,
)
from aegisops.application.investigation.advice import InvestigationAdvice, advice_from_report
from aegisops.application.investigation.contracts import InvestigationReport
from aegisops.application.investigation.model_review import (
    ModelReviewUnavailable,
    ModelReviewView,
    get_model_review,
    request_model_review,
)
from aegisops.application.investigation.request_candidates import (
    RequestCandidateObservation,
    request_candidates,
)
from aegisops.application.investigation.service import latest_report
from aegisops.application.investigation.trace_overlaps import (
    TraceOverlapObservation,
    trace_overlaps,
)
from aegisops.application.remediation.approval import (
    ApprovalDecisionInput,
    ApprovalRejected,
    ApprovalView,
    decide_approval,
    get_approval,
)
from aegisops.application.remediation.audit import ActionAttemptView, list_action_attempts
from aegisops.application.remediation.policy import PolicyDecisionView, authorize_plan
from aegisops.application.remediation.recovery import RecoveryAssessmentView, assess_lab_recovery
from aegisops.application.remediation.service import (
    PlanRejected,
    RemediationPlanView,
    get_plan,
    propose_plan,
)
from aegisops.application.request_derived_signals import (
    RequestDerivedSignalView,
    RequestSignalWindowView,
    SignalWindowInvalid,
    get_request_signal_window,
    list_request_signals,
)
from aegisops.application.request_events import (
    EventConflict,
    RequestEventIdentityUnavailable,
    RequestEventSourceUnauthorized,
    RequestFailureEventInput,
    RequestFailureEventView,
    UnknownEventService,
    authorize_request_event_source,
    events_for_trace,
    get_request_event,
    record_request_event,
    verify_request_event,
)
from aegisops.application.request_failure_cohorts import (
    CohortWindowTooLarge,
    RequestFailureCohort,
    recent_request_failure_cohorts,
)
from aegisops.application.request_signal_alerts import (
    AlertResolveInput,
    SignalPromotionInput,
    promote_signal,
    resolve_signal_alert,
)
from aegisops.application.request_signal_scheduler import DetectorRunView, list_detector_runs
from aegisops.config import Settings, require_dev_runtime
from aegisops.domain.models import IncidentStatus
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    CorrelationDecisionRecord,
    IncidentAlertRecord,
    IncidentRecord,
    IncidentRelationRecord,
    IncidentTimelineRecord,
    IncidentWorkflowStartRecord,
)
from aegisops.infrastructure.database.session import check_database
from aegisops.observability.tracing import configure_tracing


class HealthResponse(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    require_dev_runtime(settings)
    app = FastAPI(title="AegisOps API", version="0.1.0")

    @app.get("/api/v1/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/api/v1/health/ready", response_model=HealthResponse)
    def ready() -> HealthResponse:
        if not check_database(settings):
            raise HTTPException(status_code=503, detail="database unavailable")
        return HealthResponse(status="ready")

    def require_dev_token(authorization: str | None) -> None:
        if not settings.alert_webhook_token:
            raise HTTPException(status_code=503, detail="alert ingestion is not configured")
        expected = f"Bearer {settings.alert_webhook_token}"
        if authorization is None or not compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def require_operator_token(authorization: str | None) -> None:
        if not settings.operator_token:
            raise HTTPException(status_code=503, detail="operator access is not configured")
        expected = f"Bearer {settings.operator_token}"
        if authorization is None or not compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def require_request_event_token(authorization: str | None, service: str) -> None:
        try:
            authorize_request_event_source(settings, authorization, service)
        except RequestEventIdentityUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="request event ingestion is not configured"
            ) from exc
        except RequestEventSourceUnauthorized as exc:
            raise HTTPException(status_code=401, detail="unauthorized") from exc

    def require_approver_token(authorization: str | None) -> None:
        if not settings.approver_token:
            raise HTTPException(status_code=503, detail="approver access is not configured")
        expected = f"Bearer {settings.approver_token}"
        if authorization is None or not compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    async def require_direct_decision_safe(incident_id: UUID) -> None:
        with session_for(settings.database_url) as session:
            start = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
            if start is not None and start.state != "STARTED":
                raise HTTPException(
                    status_code=409,
                    detail="workflow start is unresolved; reconcile it before direct decisions",
                )
            workflow_id = start.workflow_id if start is not None else None
        if workflow_id is None:
            return
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        if description.status is not None and description.status.name == "RUNNING":
            raise HTTPException(
                status_code=409,
                detail="workflow is running; submit /workflow/decision instead",
            )

    @app.post("/api/v1/alerts/{alert_id}/resolve")
    def resolve_promoted_alert(
        alert_id: UUID, body: AlertResolveInput, authorization: str | None = Header(default=None)
    ) -> dict:
        require_operator_token(authorization)
        try:
            return resolve_signal_alert(
                settings, settings.dev_tenant_id, alert_id,
                reason=body.reason, operator_id="dev-operator",
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="alert not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/alerts/ingest", response_model=IngestResult, status_code=202)
    def ingest(
        body: AlertmanagerWebhook, authorization: str | None = Header(default=None)
    ) -> IngestResult:
        require_dev_token(authorization)
        return ingest_alerts(settings, body)

    @app.post("/api/v1/request-events", response_model=RequestFailureEventView, status_code=202)
    def ingest_request_event(
        body: RequestFailureEventInput, authorization: str | None = Header(default=None)
    ) -> RequestFailureEventView:
        require_request_event_token(authorization, body.service)
        try:
            return record_request_event(settings, body)
        except UnknownEventService as exc:
            raise HTTPException(status_code=422, detail="unknown event service") from exc
        except EventConflict as exc:
            raise HTTPException(status_code=409, detail="source event ID conflicts") from exc

    @app.get("/api/v1/request-events/{event_id}", response_model=RequestFailureEventView)
    def request_event(
        event_id: UUID, authorization: str | None = Header(default=None)
    ) -> RequestFailureEventView:
        require_dev_token(authorization)
        event = get_request_event(settings, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="request event not found")
        return event

    @app.get(
        "/api/v1/request-traces/{trace_id}/events",
        response_model=list[RequestFailureEventView],
    )
    def request_trace_events(
        trace_id: str, authorization: str | None = Header(default=None)
    ) -> list[RequestFailureEventView]:
        require_dev_token(authorization)
        try:
            return events_for_trace(settings, trace_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid trace ID") from exc

    @app.get(
        "/api/v1/request-failure-cohorts",
        response_model=list[RequestFailureCohort],
    )
    def request_failure_cohorts(
        window_minutes: int = Query(default=10, ge=1, le=60),
        min_distinct_traces: int = Query(default=2, ge=2, le=20),
        service: str | None = Query(default=None, min_length=1, max_length=80),
        authorization: str | None = Header(default=None),
    ) -> list[RequestFailureCohort]:
        require_dev_token(authorization)
        try:
            return recent_request_failure_cohorts(
                settings,
                window_minutes=window_minutes,
                min_distinct_traces=min_distinct_traces,
                service=service,
            )
        except CohortWindowTooLarge as exc:
            raise HTTPException(
                status_code=409, detail="request event window exceeds 1000 events; narrow it"
            ) from exc

    @app.get(
        "/api/v1/request-failure-signals",
        response_model=list[RequestDerivedSignalView],
    )
    def request_failure_signals(
        authorization: str | None = Header(default=None),
    ) -> list[RequestDerivedSignalView]:
        require_dev_token(authorization)
        return list_request_signals(settings)

    @app.get(
        "/api/v1/request-failure-signal-window",
        response_model=RequestSignalWindowView,
    )
    def request_failure_signal_window(
        window_start: datetime,
        authorization: str | None = Header(default=None),
    ) -> RequestSignalWindowView:
        require_dev_token(authorization)
        try:
            window = get_request_signal_window(settings, window_start)
        except SignalWindowInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if window is None:
            raise HTTPException(status_code=404, detail="signal window not finalized")
        return window

    @app.post("/api/v1/request-failure-signals/{signal_id}/promote")
    def promote_request_signal(
        signal_id: UUID, body: SignalPromotionInput,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_operator_token(authorization)
        try:
            alert_id = promote_signal(settings, signal_id, reason=body.reason,
                                      operator_id="dev-operator")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"alert_id": str(alert_id), "source_status": "OBSERVED",
                "root_cause_status": "UNDETERMINED"}

    @app.get(
        "/api/v1/request-failure-detector/runs",
        response_model=list[DetectorRunView],
    )
    def request_failure_detector_runs(
        limit: int = Query(default=100, ge=1, le=100),
        compensation: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> list[DetectorRunView]:
        require_dev_token(authorization)
        return list_detector_runs(settings, limit=limit, compensation=compensation)

    @app.get(
        "/api/v1/request-events/{event_id}/verification",
        response_model=RequestFailureEventView,
    )
    def request_event_verification(
        event_id: UUID, authorization: str | None = Header(default=None)
    ) -> RequestFailureEventView:
        require_dev_token(authorization)
        event = verify_request_event(settings, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="request event not found")
        return event

    @app.post("/api/v1/change-events", response_model=ChangeEventView, status_code=201)
    def create_change_event(
        body: ChangeEventInput, authorization: str | None = Header(default=None)
    ) -> ChangeEventView:
        require_operator_token(authorization)
        try:
            return record_change(settings, body)
        except UnknownService as exc:
            raise HTTPException(status_code=404, detail="service not found in catalog") from exc
        except ChangeConflict as exc:
            raise HTTPException(
                status_code=409, detail="change event ID conflicts with existing facts"
            ) from exc

    @app.get("/api/v1/incidents", response_model=list[IncidentSummary])
    def incidents(authorization: str | None = Header(default=None)) -> list[IncidentSummary]:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            records = session.scalars(
                select(IncidentRecord)
                .where(IncidentRecord.tenant_id == settings.dev_tenant_id)
                .order_by(IncidentRecord.opened_at.desc())
                .limit(100)
            ).all()
            return [
                IncidentSummary(
                    id=record.id,
                    title=record.title,
                    status=record.status,
                    processing_phase=record.processing_phase,
                    severity=record.severity,
                    version=record.version,
                    opened_at=record.opened_at,
                    alert_count=session.scalar(
                        select(func.count(IncidentAlertRecord.id)).where(
                            IncidentAlertRecord.tenant_id == settings.dev_tenant_id,
                            IncidentAlertRecord.incident_id == record.id,
                        )
                    )
                    or 0,
                )
                for record in records
            ]

    @app.get("/api/v1/incidents/{incident_id}", response_model=IncidentSummary)
    def incident_detail(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> IncidentSummary:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            record = session.scalar(
                select(IncidentRecord).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )
            if record is None:
                raise HTTPException(status_code=404, detail="incident not found")
            count = session.scalar(
                select(func.count(IncidentAlertRecord.id)).where(
                    IncidentAlertRecord.tenant_id == settings.dev_tenant_id,
                    IncidentAlertRecord.incident_id == incident_id,
                )
            )
            return IncidentSummary(
                id=record.id,
                title=record.title,
                status=record.status,
                processing_phase=record.processing_phase,
                severity=record.severity,
                version=record.version,
                opened_at=record.opened_at,
                alert_count=count or 0,
            )

    @app.get(
        "/api/v1/incidents/{incident_id}/request-evidence",
        response_model=list[IncidentRequestEvidenceView],
    )
    def incident_request_evidence(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[IncidentRequestEvidenceView]:
        require_dev_token(authorization)
        try:
            return list_request_evidence(settings, incident_id)
        except IncidentNotFound as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc

    @app.post(
        "/api/v1/incidents/{incident_id}/request-evidence",
        response_model=IncidentRequestEvidenceView,
        status_code=201,
    )
    def attach_incident_request_evidence(
        incident_id: UUID,
        body: AttachRequestEvidenceInput,
        authorization: str | None = Header(default=None),
    ) -> IncidentRequestEvidenceView:
        require_operator_token(authorization)
        try:
            return attach_request_evidence(
                settings, incident_id, body, operator_id="dev-operator"
            )
        except (IncidentNotFound, RequestEvidenceNotFound) as exc:
            raise HTTPException(status_code=404, detail="incident or event not found") from exc
        except IncidentVersionConflict as exc:
            raise HTTPException(status_code=409, detail="incident version changed") from exc
        except RequestEvidenceNotVerified as exc:
            raise HTTPException(
                status_code=409, detail="request span fact is not verified"
            ) from exc
        except RequestEvidenceConflict as exc:
            raise HTTPException(status_code=409, detail="request evidence already linked") from exc

    def operator_decision(
        incident_id: UUID,
        body: OperatorDecision,
        authorization: str | None,
        target: IncidentStatus,
    ) -> IncidentSummary:
        require_operator_token(authorization)
        try:
            changed = change_incident_status(
                settings,
                incident_id,
                expected_version=body.expected_version,
                target=target,
                reason=body.reason,
                operator_id="dev-operator",
            )
        except IncidentNotFound as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc
        except IncidentVersionConflict as exc:
            raise HTTPException(status_code=409, detail="incident version changed") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with session_for(settings.database_url) as session:
            count = session.scalar(
                select(func.count(IncidentAlertRecord.id)).where(
                    IncidentAlertRecord.tenant_id == settings.dev_tenant_id,
                    IncidentAlertRecord.incident_id == incident_id,
                )
            )
        return IncidentSummary(
            id=changed.id,
            title=changed.title,
            status=changed.status.value,
            processing_phase=changed.processing_phase.value,
            severity=changed.severity,
            version=changed.version,
            opened_at=changed.opened_at,
            alert_count=count or 0,
        )

    @app.post("/api/v1/incidents/{incident_id}/cancel", response_model=IncidentSummary)
    async def cancel_incident(
        incident_id: UUID,
        body: OperatorDecision,
        authorization: str | None = Header(default=None),
    ) -> IncidentSummary:
        require_operator_token(authorization)
        await require_direct_decision_safe(incident_id)
        return operator_decision(incident_id, body, authorization, IncidentStatus.CANCELLED)

    @app.post("/api/v1/incidents/{incident_id}/escalate", response_model=IncidentSummary)
    async def escalate_incident(
        incident_id: UUID,
        body: OperatorDecision,
        authorization: str | None = Header(default=None),
    ) -> IncidentSummary:
        require_operator_token(authorization)
        await require_direct_decision_safe(incident_id)
        try:
            changed = escalate_incident_phase(
                settings,
                incident_id,
                expected_version=body.expected_version,
                reason=body.reason,
                operator_id="dev-operator",
            )
        except IncidentNotFound as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc
        except IncidentVersionConflict as exc:
            raise HTTPException(status_code=409, detail="incident version changed") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with session_for(settings.database_url) as session:
            count = session.scalar(
                select(func.count(IncidentAlertRecord.id)).where(
                    IncidentAlertRecord.tenant_id == settings.dev_tenant_id,
                    IncidentAlertRecord.incident_id == incident_id,
                )
            )
        return IncidentSummary(
            id=changed.id,
            title=changed.title,
            status=changed.status.value,
            processing_phase=changed.processing_phase.value,
            severity=changed.severity,
            version=changed.version,
            opened_at=changed.opened_at,
            alert_count=count or 0,
        )

    @app.post("/api/v1/incidents/{incident_id}/merge", response_model=IncidentCorrectionResult)
    def merge_incident(
        incident_id: UUID,
        body: MergeIncidentsInput,
        authorization: str | None = Header(default=None),
    ) -> IncidentCorrectionResult:
        require_operator_token(authorization)
        try:
            target_id, count = merge_incidents(
                settings,
                incident_id,
                body.target_incident_id,
                source_version=body.expected_source_version,
                target_version=body.expected_target_version,
                reason=body.reason,
                operator_id="dev-operator",
            )
        except IncidentNotFound as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc
        except IncidentVersionConflict as exc:
            raise HTTPException(status_code=409, detail="incident version changed") from exc
        except IncidentCorrectionRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return IncidentCorrectionResult(incident_id=target_id, alerts_moved=count)

    @app.post("/api/v1/incidents/{incident_id}/split", response_model=IncidentCorrectionResult)
    def split_incident_route(
        incident_id: UUID,
        body: SplitIncidentInput,
        authorization: str | None = Header(default=None),
    ) -> IncidentCorrectionResult:
        require_operator_token(authorization)
        try:
            new_id, count = split_incident(
                settings,
                incident_id,
                expected_version=body.expected_version,
                alert_ids=body.alert_ids,
                title=body.title,
                reason=body.reason,
                operator_id="dev-operator",
            )
        except IncidentNotFound as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc
        except IncidentVersionConflict as exc:
            raise HTTPException(status_code=409, detail="incident version changed") from exc
        except IncidentCorrectionRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return IncidentCorrectionResult(incident_id=new_id, alerts_moved=count)

    @app.get(
        "/api/v1/incidents/{incident_id}/relations",
        response_model=list[IncidentRelationView],
    )
    def incident_relations(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[IncidentRelationView]:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            exists = session.scalar(
                select(IncidentRecord.id).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="incident not found")
            records = session.scalars(
                select(IncidentRelationRecord)
                .where(
                    IncidentRelationRecord.tenant_id == settings.dev_tenant_id,
                    (IncidentRelationRecord.from_incident_id == incident_id)
                    | (IncidentRelationRecord.to_incident_id == incident_id),
                )
                .order_by(IncidentRelationRecord.created_at, IncidentRelationRecord.id)
            ).all()
            return [
                IncidentRelationView(
                    from_incident_id=row.from_incident_id,
                    to_incident_id=row.to_incident_id,
                    relation_type=row.relation_type,
                    operator_id=row.operator_id,
                    reason=row.reason,
                    created_at=row.created_at,
                )
                for row in records
            ]

    @app.get(
        "/api/v1/incidents/{incident_id}/trace-overlaps",
        response_model=list[TraceOverlapObservation],
    )
    def incident_trace_overlaps(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[TraceOverlapObservation]:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            exists = session.scalar(
                select(IncidentRecord.id).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )
        if exists is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return trace_overlaps(settings, settings.dev_tenant_id, incident_id)

    @app.get("/api/v1/incidents/{incident_id}/request-candidates",
             response_model=RequestCandidateObservation)
    def incident_request_candidates(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> RequestCandidateObservation:
        require_dev_token(authorization)
        try:
            return request_candidates(settings, settings.dev_tenant_id, incident_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="incident not found") from exc

    @app.get("/api/v1/incidents/{incident_id}/timeline", response_model=list[TimelineItem])
    def incident_timeline(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[TimelineItem]:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            records = session.scalars(
                select(IncidentTimelineRecord)
                .where(
                    IncidentTimelineRecord.tenant_id == settings.dev_tenant_id,
                    IncidentTimelineRecord.incident_id == incident_id,
                )
                .order_by(IncidentTimelineRecord.created_at, IncidentTimelineRecord.id)
            ).all()
            return [
                TimelineItem(
                    id=record.id,
                    event_type=record.event_type,
                    summary=record.summary,
                    details=record.details,
                    created_at=record.created_at,
                )
                for record in records
            ]

    @app.get("/api/v1/incidents/{incident_id}/workflow", response_model=IncidentWorkflowView)
    async def incident_workflow(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> IncidentWorkflowView:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            exists = session.scalar(
                select(IncidentRecord.id).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )
            start_record = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
            start_state = start_record.state if start_record else "UNRECORDED"
        if exists is None:
            raise HTTPException(status_code=404, detail="incident not found")
        workflow_id = workflow_id_for(settings.dev_tenant_id, incident_id)
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return IncidentWorkflowView(
                    workflow_id=workflow_id,
                    start_state=start_state,
                    run_id=None,
                    status="NOT_FOUND_IN_TEMPORAL",
                    review_state=None,
                    started_at=None,
                    closed_at=None,
                )
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        review_state = None
        if description.status is not None and description.status.name == "RUNNING":
            try:
                review_state = await client.get_workflow_handle(workflow_id).query("review_state")
            except RPCError:
                review_state = "QUERY_UNAVAILABLE"
        return IncidentWorkflowView(
            workflow_id=workflow_id,
            start_state=start_state,
            run_id=description.run_id,
            status=description.status.name if description.status else "UNKNOWN",
            review_state=review_state,
            started_at=description.start_time,
            closed_at=description.close_time,
        )

    @app.post("/api/v1/incidents/{incident_id}/workflow/decision", status_code=202)
    async def submit_workflow_decision(
        incident_id: UUID,
        body: OperatorWorkflowDecision,
        authorization: str | None = Header(default=None),
    ) -> dict[str, bool]:
        require_operator_token(authorization)
        with session_for(settings.database_url) as session:
            start = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
        if start is None or start.state != "STARTED":
            raise HTTPException(status_code=409, detail="incident workflow is not started")
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            await client.get_workflow_handle(start.workflow_id).signal(
                "operator_decision",
                args=[body.decision, body.expected_version, body.reason, "dev-operator"],
            )
        except RPCError as exc:
            if exc.status in {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}:
                raise HTTPException(status_code=409, detail="workflow is not running") from exc
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        return {"accepted": True}

    @app.post(
        "/api/v1/incidents/{incident_id}/workflow/reconcile",
        response_model=IncidentWorkflowView,
    )
    async def reconcile_workflow_start(
        incident_id: UUID,
        body: ReconcileWorkflowStartInput,
        authorization: str | None = Header(default=None),
    ) -> IncidentWorkflowView:
        require_operator_token(authorization)
        with session_for(settings.database_url) as session:
            row = session.scalar(
                select(IncidentWorkflowStartRecord).where(
                    IncidentWorkflowStartRecord.tenant_id == settings.dev_tenant_id,
                    IncidentWorkflowStartRecord.incident_id == incident_id,
                )
            )
            if row is None:
                raise HTTPException(status_code=404, detail="workflow start record not found")
            if row.version != body.expected_start_version or row.state != "RECONCILIATION_REQUIRED":
                raise HTTPException(status_code=409, detail="workflow start state changed")
            workflow_id = row.workflow_id
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=409,
                    detail="Temporal workflow not found; start remains unresolved",
                ) from exc
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Temporal unavailable") from exc
        outcome = reconcile_existing_workflow_start(
            settings,
            settings.dev_tenant_id,
            incident_id,
            expected_start_version=body.expected_start_version,
            run_id=description.run_id,
            started_at=description.start_time,
            reason=body.reason,
            operator_id="dev-operator",
        )
        if outcome != "STARTED":
            raise HTTPException(status_code=409, detail=f"workflow start {outcome.lower()}")
        review_state = None
        if description.status is not None and description.status.name == "RUNNING":
            try:
                review_state = await client.get_workflow_handle(workflow_id).query("review_state")
            except RPCError:
                review_state = "QUERY_UNAVAILABLE"
        return IncidentWorkflowView(
            workflow_id=workflow_id,
            start_state="STARTED",
            run_id=description.run_id,
            status=description.status.name if description.status else "UNKNOWN",
            review_state=review_state,
            started_at=description.start_time,
            closed_at=description.close_time,
        )

    @app.get(
        "/api/v1/incidents/{incident_id}/investigation",
        response_model=InvestigationReport,
    )
    def incident_investigation(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> InvestigationReport:
        require_dev_token(authorization)
        report = latest_report(settings, settings.dev_tenant_id, incident_id)
        if report is None:
            raise HTTPException(status_code=404, detail="investigation report not ready")
        return report

    @app.get(
        "/api/v1/incidents/{incident_id}/investigation/advice",
        response_model=InvestigationAdvice,
    )
    def incident_investigation_advice(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> InvestigationAdvice:
        require_dev_token(authorization)
        report = latest_report(settings, settings.dev_tenant_id, incident_id)
        if report is None:
            raise HTTPException(status_code=404, detail="investigation report not ready")
        return advice_from_report(report)

    @app.get(
        "/api/v1/incidents/{incident_id}/investigation/model-review",
        response_model=ModelReviewView,
    )
    def incident_model_review(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> ModelReviewView:
        require_dev_token(authorization)
        review = get_model_review(settings, settings.dev_tenant_id, incident_id)
        if review is None:
            raise HTTPException(status_code=404, detail="model review not found")
        return review

    @app.post(
        "/api/v1/incidents/{incident_id}/investigation/model-review",
        response_model=ModelReviewView,
    )
    def create_model_review(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> ModelReviewView:
        require_operator_token(authorization)
        try:
            return request_model_review(settings, settings.dev_tenant_id, incident_id)
        except ModelReviewUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/v1/incidents/{incident_id}/remediation-plan",
        response_model=RemediationPlanView,
    )
    def incident_remediation_plan(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> RemediationPlanView:
        require_dev_token(authorization)
        proposal = get_plan(settings, settings.dev_tenant_id, incident_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="remediation plan not found")
        return proposal

    @app.get(
        "/api/v1/incidents/{incident_id}/actions/{action_id}/attempts",
        response_model=list[ActionAttemptView],
    )
    def action_attempts(
        incident_id: UUID, action_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[ActionAttemptView]:
        require_operator_token(authorization)
        attempts = list_action_attempts(settings, settings.dev_tenant_id, incident_id, action_id)
        if attempts is None:
            raise HTTPException(status_code=404, detail="action not found in incident")
        return attempts

    @app.get(
        "/api/v1/incidents/{incident_id}/recovery-assessment",
        response_model=RecoveryAssessmentView,
    )
    def recovery_assessment(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> RecoveryAssessmentView:
        require_operator_token(authorization)
        assessment = assess_lab_recovery(settings, settings.dev_tenant_id, incident_id)
        if assessment is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return assessment

    @app.post(
        "/api/v1/incidents/{incident_id}/remediation-plan",
        response_model=RemediationPlanView,
        status_code=201,
    )
    def propose_remediation_plan(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> RemediationPlanView:
        require_operator_token(authorization)
        try:
            return propose_plan(settings, settings.dev_tenant_id, incident_id)
        except PlanRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/v1/incidents/{incident_id}/remediation-plan/authorize",
        response_model=PolicyDecisionView,
    )
    def authorize_remediation_plan(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> PolicyDecisionView:
        require_operator_token(authorization)
        try:
            return authorize_plan(settings, settings.dev_tenant_id, incident_id, "dev-operator")
        except PlanRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/approvals/{approval_id}", response_model=ApprovalView)
    def read_approval(
        approval_id: UUID, authorization: str | None = Header(default=None)
    ) -> ApprovalView:
        require_approver_token(authorization)
        approval = get_approval(settings, settings.dev_tenant_id, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    @app.post("/api/v1/approvals/{approval_id}/decision", response_model=ApprovalView)
    def submit_approval_decision(
        approval_id: UUID,
        body: ApprovalDecisionInput,
        authorization: str | None = Header(default=None),
    ) -> ApprovalView:
        require_approver_token(authorization)
        try:
            return decide_approval(
                settings,
                settings.dev_tenant_id,
                approval_id,
                body,
                "dev-approver",
            )
        except ApprovalRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/v1/incidents/{incident_id}/correlation-decisions",
        response_model=list[CorrelationDecisionView],
    )
    def correlation_decisions(
        incident_id: UUID, authorization: str | None = Header(default=None)
    ) -> list[CorrelationDecisionView]:
        require_dev_token(authorization)
        with session_for(settings.database_url) as session:
            incident = session.scalar(
                select(IncidentRecord.id).where(
                    IncidentRecord.tenant_id == settings.dev_tenant_id,
                    IncidentRecord.id == incident_id,
                )
            )
            if incident is None:
                raise HTTPException(status_code=404, detail="incident not found")
            records = session.scalars(
                select(CorrelationDecisionRecord)
                .where(
                    CorrelationDecisionRecord.tenant_id == settings.dev_tenant_id,
                    CorrelationDecisionRecord.result_incident_id == incident_id,
                )
                .order_by(CorrelationDecisionRecord.decided_at, CorrelationDecisionRecord.id)
            ).all()
            return [
                CorrelationDecisionView(
                    alert_id=row.alert_id,
                    candidate_incident_id=row.candidate_incident_id,
                    result_incident_id=row.result_incident_id,
                    decision=row.decision,
                    total_score=float(row.total_score),
                    matched_features=row.matched_features,
                    conflicting_features=row.conflicting_features,
                    rule_version=row.rule_version,
                    decided_at=row.decided_at,
                )
                for row in records
            ]

    configure_tracing(app, enabled=settings.otel_enabled)
    return app


app = create_app()
