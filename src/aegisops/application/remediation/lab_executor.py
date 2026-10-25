"""A separate Lab-only executor for allowlisted cache and service recovery actions."""

import socket
import time
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aegisops.config import Profile, Settings
from aegisops.domain.models import ActionSnapshot
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    ActionAttemptRecord,
    ActionRecord,
    ApprovalRecord,
    IncidentRecord,
    IncidentTimelineRecord,
    PolicyDecisionRecord,
    RemediationPlanRecord,
    ResourceLeaseRecord,
    ResourceRecord,
    ServiceRecord,
)

from .lab_rollback import (
    LabOrderRollbackAdapter,
    RollbackPreconditionFailed,
    RollbackReadiness,
    _expected,
    fingerprint_from_inspect,
)
from .policy import PolicyResult, _query_opa
from .restart_preconditions import (
    MAX_PREFLIGHT_AGE_SECONDS,
    RESTART_TARGETS,
    RestartIdentity,
    RestartPreconditionFailed,
    restart_identity,
)

EXECUTOR_ID = "lab-executor"
LEASE_HOURS = 12


class LabReadiness(BaseModel):
    checked_at: datetime
    redis_ping: bool
    payment_ready: bool

    @property
    def passed(self) -> bool:
        return self.redis_ping and self.payment_ready


class OrderReadiness(BaseModel):
    checked_at: datetime
    order_ready: bool
    payment_ready: bool

    @property
    def passed(self) -> bool:
        return self.order_ready and self.payment_ready


class LabDockerAdapter:
    """Docker socket exists only in the Lab executor container, never in API or Agent."""

    def __init__(
        self, socket_path: str = "/var/run/docker.sock", redis_host: str = "redis"
    ) -> None:
        self.socket_path = socket_path
        self.redis_host = redis_host
        self._restart_preflight: dict[str, tuple[RestartIdentity, float]] = {}

    def _inspect_restart(self, http: httpx.Client, action_type: str) -> RestartIdentity:
        response = http.get(f"http://docker/containers/{RESTART_TARGETS[action_type][1]}/json")
        response.raise_for_status()
        return restart_identity(response.json(), action_type)

    def _restart_condition(self, action_type: str) -> bool:
        return (not self.redis_healthy() if action_type == "restart_cache"
                else self.order_pool_exhausted())

    def preflight_restart(self, action_type: str) -> bool:
        self._restart_preflight.pop(action_type, None)
        try:
            transport = httpx.HTTPTransport(uds=self.socket_path)
            with httpx.Client(transport=transport, timeout=5) as http:
                identity = self._inspect_restart(http, action_type)
                if not self._restart_condition(action_type):
                    return False
                if self._inspect_restart(http, action_type) != identity:
                    return False
            self._restart_preflight[action_type] = (identity, time.monotonic())
            return True
        except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError):
            return False

    def _restart_verified(self, action_type: str) -> None:
        expected = self._restart_preflight.pop(action_type, None)
        if expected is None:
            raise RestartPreconditionFailed("restart requires a verified preflight")
        identity, checked_at = expected
        with httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path), timeout=15) as http:
            try:
                if (time.monotonic() - checked_at >= MAX_PREFLIGHT_AGE_SECONDS
                        or self._inspect_restart(http, action_type) != identity
                        or not self._restart_condition(action_type)
                        or self._inspect_restart(http, action_type) != identity
                        or time.monotonic() - checked_at >= MAX_PREFLIGHT_AGE_SECONDS):
                    raise RestartPreconditionFailed(
                        "restart identity, condition or freshness changed")
            except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
                raise RestartPreconditionFailed(
                    "restart preflight could not be reverified") from exc
            response = http.post(f"http://docker/containers/{identity.container_id}/restart",
                                 params={"t": 3})
            response.raise_for_status()

    def redis_healthy(self) -> bool:
        try:
            with socket.create_connection((self.redis_host, 6379), timeout=1) as connection:
                connection.sendall(b"*1\r\n$4\r\nPING\r\n")
                return connection.recv(64).startswith(b"+PONG")
        except OSError:
            return False

    def restart_redis(self) -> None:
        self._restart_verified("restart_cache")

    def restart_order(self) -> None:
        self._restart_verified("restart_service")

    def order_pool_exhausted(self) -> bool:
        try:
            response = httpx.get("http://order-service:8000/health/ready", timeout=4)
            return (
                response.status_code == 503
                and response.json().get("detail") == "order database pool exhausted"
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def redis_started_at(self) -> datetime:
        return self._container_started_at("aegisops-redis-1")

    def order_started_at(self) -> datetime:
        return self._container_started_at("aegisops-order-service-1")

    def _container_started_at(self, container_name: str) -> datetime:
        with httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path), timeout=5) as http:
            response = http.get(f"http://docker/containers/{container_name}/json")
            response.raise_for_status()
            value = response.json()["State"]["StartedAt"]
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def verify_redis(self) -> LabReadiness:
        result = LabReadiness(
            checked_at=datetime.now(UTC), redis_ping=False, payment_ready=False
        )
        for _ in range(20):
            redis_ping = self.redis_healthy()
            payment_ready = False
            if redis_ping:
                try:
                    response = httpx.get("http://payment-service:8000/health/ready", timeout=2)
                    payment_ready = (
                        response.status_code == 200 and response.json().get("status") == "ready"
                    )
                except (httpx.HTTPError, ValueError):
                    pass
            result = LabReadiness(
                checked_at=datetime.now(UTC),
                redis_ping=redis_ping,
                payment_ready=payment_ready,
            )
            if result.passed:
                return result
            time.sleep(1)
        return result

    def verify_order(self) -> OrderReadiness:
        result = OrderReadiness(
            checked_at=datetime.now(UTC), order_ready=False, payment_ready=False
        )
        for _ in range(20):
            order_ready = False
            payment_ready = False
            try:
                response = httpx.get("http://order-service:8000/health/ready", timeout=3)
                order_ready = (
                    response.status_code == 200 and response.json().get("status") == "ready"
                )
                response = httpx.get("http://payment-service:8000/health/ready", timeout=3)
                payment_ready = (
                    response.status_code == 200 and response.json().get("status") == "ready"
                )
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            result = OrderReadiness(
                checked_at=datetime.now(UTC),
                order_ready=order_ready,
                payment_ready=payment_ready,
            )
            if result.passed:
                return result
            time.sleep(1)
        return result


def _execution_context(settings: Settings, tenant_id: UUID, action_id: UUID) -> dict | None:
    with session_for(settings.database_url) as session:
        action = session.scalar(
            select(ActionRecord).where(
                ActionRecord.tenant_id == tenant_id, ActionRecord.id == action_id
            )
        )
        if action is None or action.status != "AUTHORIZED":
            return None
        plan = session.scalar(
            select(RemediationPlanRecord).where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == action.plan_id,
            )
        )
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == action.incident_id
            )
        )
        approval = session.scalar(
            select(ApprovalRecord)
            .where(
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.action_id == action_id,
                ApprovalRecord.status == "APPROVED",
            )
            .order_by(ApprovalRecord.created_at.desc())
            .limit(1)
        )
        if plan is None or incident is None or approval is None:
            return None
        snapshot = ActionSnapshot.model_validate(action.snapshot)
        try:
            target_kind, target_id_text = snapshot.target.split("/", 1)
            target_id = UUID(target_id_text)
        except ValueError:
            return None
        target_model: type[ResourceRecord] | type[ServiceRecord]
        if (snapshot.action_type, target_kind) == ("restart_cache", "resource"):
            target_model, target_slug = ResourceRecord, "redis-main"
        elif (snapshot.action_type, target_kind) in {
            ("restart_service", "service"),
            ("rollback_service", "service"),
        }:
            target_model, target_slug = ServiceRecord, "order-service"
        else:
            return None
        target = cast(
            ResourceRecord | ServiceRecord | None,
            session.scalar(
                select(target_model).where(
                    target_model.tenant_id == tenant_id, target_model.id == target_id
                )
            )
        )
        if target is None:
            return None
        now = datetime.now(UTC)
        if not (
            settings.profile == Profile.DEV
            and not settings.remediation_kill_switch
            and plan.status == "AUTHORIZED"
            and incident.status == "OPEN"
            and incident.version == plan.source_incident_version
            and plan.plan_hash == snapshot.plan_hash
            and target.slug == target_slug
            and target.environment == "lab"
            and target.version == snapshot.resource_version
            and snapshot.schema_version == 1
            and approval.expires_at > now
            and approval.snapshot_hash == snapshot.digest()
            and approval.decided_by is not None
            and approval.requested_by != approval.decided_by
            and approval.policy_decision_id is not None
        ):
            return None
        policy = session.scalar(
            select(PolicyDecisionRecord).where(
                PolicyDecisionRecord.tenant_id == tenant_id,
                PolicyDecisionRecord.id == approval.policy_decision_id,
            )
        )
        if (
            policy is None
            or policy.outcome != "REQUIRE_APPROVAL"
            or policy.action_id != action_id
            or policy.plan_hash != plan.plan_hash
            or policy.snapshot_hash != snapshot.digest()
        ):
            return None
        return {
            "tenant_id": tenant_id,
            "incident_id": incident.id,
            "incident_version": incident.version,
            "action_id": action_id,
            "plan_id": plan.id,
            "plan_hash": plan.plan_hash,
            "resource_id": target.id,
            "resource_version": target.version,
            "target_kind": target_kind,
            "target_slug": target_slug,
            "snapshot": snapshot,
            "snapshot_hash": snapshot.digest(),
            "approval_id": approval.id,
            "approval_expires_at": approval.expires_at,
        }


def _execution_policy(settings: Settings, context: dict) -> tuple[PolicyResult, dict]:
    snapshot: ActionSnapshot = context["snapshot"]
    now = datetime.now(UTC)
    policy_input = {
        "stage": "EXECUTE",
        "actor": {"id": EXECUTOR_ID, "role": "Executor"},
        "tenant": {"id": str(context["tenant_id"])},
        "incident": {
            "id": str(context["incident_id"]),
            "status": "OPEN",
            "version": context["incident_version"],
        },
        "plan": {
            "id": str(context["plan_id"]),
            "status": "AUTHORIZED",
            "source_incident_version": context["incident_version"],
            "is_stale": False,
            "plan_hash": context["plan_hash"],
        },
        "action": {
            "id": str(context["action_id"]),
            "status": "AUTHORIZED",
            "snapshot_hash": context["snapshot_hash"],
            "snapshot": snapshot.model_dump(mode="json"),
        },
        "resource": {
            "kind": context["target_kind"],
            "slug": context["target_slug"],
            "version": context["resource_version"],
        },
        "environment": {"name": "lab"},
        "risk": {"level": snapshot.risk},
        "approval": {
            "status": "APPROVED",
            "snapshot_hash": context["snapshot_hash"],
            "expires_at_unix": int(context["approval_expires_at"].timestamp()),
        },
        "runtime": {
            "kill_switch": settings.remediation_kill_switch,
            "now_unix": int(now.timestamp()),
        },
    }
    try:
        result = _query_opa(settings, policy_input)
    except (httpx.HTTPError, ValueError, TypeError):
        result = PolicyResult(
            outcome="DENY",
            reason="OPA unavailable or returned an invalid execution decision",
            policy_version="unavailable",
        )
    return result, policy_input


def _record_execution_policy(
    settings: Settings, context: dict, result: PolicyResult, data: dict
) -> None:
    with session_for(settings.database_url) as session, session.begin():
        session.add(
            PolicyDecisionRecord(
                tenant_id=context["tenant_id"],
                action_id=context["action_id"],
                plan_hash=context["plan_hash"],
                snapshot_hash=context["snapshot_hash"],
                policy_version=result.policy_version,
                outcome=result.outcome,
                reason=result.reason,
                required_role=result.required_role or None,
                required_approvals=result.required_approvals,
                input=data,
            )
        )


def _finish(
    settings: Settings,
    tenant_id: UUID,
    action_id: UUID,
    attempt_id: UUID | None,
    *,
    status: str,
    reason: str,
    expected_status: str | None = None,
    verification: LabReadiness | OrderReadiness | RollbackReadiness | None = None,
) -> bool:
    with session_for(settings.database_url) as session, session.begin():
        action = session.scalar(
            select(ActionRecord)
            .where(ActionRecord.tenant_id == tenant_id, ActionRecord.id == action_id)
            .with_for_update()
        )
        if action is None:
            return False
        expected_status = expected_status or (
            "EXECUTING" if attempt_id is not None else "AUTHORIZED"
        )
        if action.status != expected_status:
            return False
        now = datetime.now(UTC)
        action.status = status
        action.updated_at = now
        action.version += 1
        plan = session.scalar(
            select(RemediationPlanRecord)
            .where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == action.plan_id,
            )
            .with_for_update()
        )
        if plan is not None:
            plan.status = status
            plan.updated_at = now
            plan.version += 1
        if attempt_id is not None:
            attempt = session.scalar(
                select(ActionAttemptRecord)
                .where(
                    ActionAttemptRecord.tenant_id == tenant_id, ActionAttemptRecord.id == attempt_id
                )
                .with_for_update()
            )
            if attempt is not None:
                attempt.state = status
                attempt.result = {
                    "reason": reason,
                    **(
                        {"verification": verification.model_dump(mode="json")}
                        if verification is not None
                        else {}
                    ),
                }
                attempt.finished_at = now
                attempt.updated_at = now
                attempt.version += 1
        lease = session.scalar(
            select(ResourceLeaseRecord)
            .where(
                ResourceLeaseRecord.tenant_id == tenant_id,
                ResourceLeaseRecord.resource_key
                == ActionSnapshot.model_validate(action.snapshot).target,
            )
            .with_for_update()
        )
        if (
            lease is not None
            and lease.holder_action_id == action_id
            and status != "RECONCILIATION_REQUIRED"
        ):
            lease.holder_action_id = None
            lease.expires_at = None
            lease.updated_at = now
            lease.version += 1
        session.add(
            IncidentTimelineRecord(
                tenant_id=tenant_id,
                incident_id=action.incident_id,
                event_type="ACTION_EXECUTION_STATE",
                summary=f"Action {status}",
                details={
                    "action_id": str(action_id),
                    "attempt_id": str(attempt_id) if attempt_id else None,
                    "reason": reason,
                },
            )
        )
        return True


def execute_authorized_lab(
    settings: Settings, tenant_id: UUID, action_id: UUID, adapter: LabDockerAdapter
) -> str:
    context = _execution_context(settings, tenant_id, action_id)
    if context is None:
        return "SKIPPED_NOT_AUTHORIZED"
    action_type = context["snapshot"].action_type
    rollback_adapter = LabOrderRollbackAdapter(socket_path=adapter.socket_path)
    policy, policy_input = _execution_policy(settings, context)
    _record_execution_policy(settings, context, policy, policy_input)
    if policy.outcome != "ALLOW":
        _finish(settings, tenant_id, action_id, None, status="BLOCKED", reason=policy.reason)
        return "BLOCKED"
    if action_type in {"restart_cache", "restart_service"}:
        precondition_met = adapter.preflight_restart(action_type)
    else:
        precondition_met = rollback_adapter.preflight(context["snapshot"])
    if not precondition_met:
        _finish(
            settings,
            tenant_id,
            action_id,
            None,
            status="PRECONDITION_FAILED",
            reason=(
                "Redis restart identity, exited state or unavailability was not verified"
                if action_type == "restart_cache"
                else (
                    "Order restart identity, running state or DB pool exhaustion was not verified"
                    if action_type == "restart_service"
                    else "Order deployment differs from the approved rollback snapshot"
                )
            ),
        )
        return "PRECONDITION_FAILED"
    now = datetime.now(UTC)
    attempt_id = uuid4()
    with session_for(settings.database_url) as session, session.begin():
        action = session.scalar(
            select(ActionRecord)
            .where(ActionRecord.tenant_id == tenant_id, ActionRecord.id == action_id)
            .with_for_update()
        )
        incident = session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.tenant_id == tenant_id, IncidentRecord.id == context["incident_id"]
            )
            .with_for_update()
        )
        approval = session.scalar(
            select(ApprovalRecord)
            .where(
                ApprovalRecord.tenant_id == tenant_id, ApprovalRecord.id == context["approval_id"]
            )
            .with_for_update()
        )
        plan = session.scalar(
            select(RemediationPlanRecord)
            .where(
                RemediationPlanRecord.tenant_id == tenant_id,
                RemediationPlanRecord.id == context["plan_id"],
            )
            .with_for_update()
        )
        target_model: type[ResourceRecord] | type[ServiceRecord] = (
            ResourceRecord if context["target_kind"] == "resource" else ServiceRecord
        )
        target_record = cast(
            ResourceRecord | ServiceRecord | None,
            session.scalar(
                select(target_model)
                .where(
                    target_model.tenant_id == tenant_id,
                    target_model.id == context["resource_id"],
                )
                .with_for_update()
            )
        )
        if (
            action is None
            or incident is None
            or approval is None
            or plan is None
            or target_record is None
        ):
            return "SKIPPED_CHANGED"
        if not (
            action.status == "AUTHORIZED"
            and plan.status == "AUTHORIZED"
            and plan.plan_hash == context["plan_hash"]
            and incident.version == context["incident_version"]
            and approval.status == "APPROVED"
            and approval.expires_at > now
            and approval.snapshot_hash == context["snapshot_hash"]
            and target_record.version == context["resource_version"]
            and target_record.slug == context["target_slug"]
            and not settings.remediation_kill_switch
            and ActionSnapshot.model_validate(action.snapshot).digest() == context["snapshot_hash"]
        ):
            return "SKIPPED_CHANGED"
        target = context["snapshot"].target
        session.execute(
            insert(ResourceLeaseRecord)
            .values(
                tenant_id=tenant_id,
                resource_key=target,
                fencing_token=0,
            )
            .on_conflict_do_nothing(constraint="uq_resource_lease_key")
        )
        lease = session.scalar(
            select(ResourceLeaseRecord)
            .where(
                ResourceLeaseRecord.tenant_id == tenant_id,
                ResourceLeaseRecord.resource_key == target,
            )
            .with_for_update()
        )
        assert lease is not None
        # Expiration is an operator alert, not permission to reuse a resource
        # whose previous side effect may still be unknown.
        if lease.holder_action_id is not None:
            return "LEASE_HELD"
        existing_attempt = session.scalar(
            select(ActionAttemptRecord.id).where(
                ActionAttemptRecord.tenant_id == tenant_id,
                ActionAttemptRecord.action_id == action_id,
            )
        )
        if existing_attempt is not None:
            return "ALREADY_ATTEMPTED"
        lease.holder_action_id = action_id
        lease.fencing_token += 1
        lease.expires_at = now + timedelta(hours=LEASE_HOURS)
        lease.updated_at = now
        lease.version += 1
        session.add(
            ActionAttemptRecord(
                id=attempt_id,
                tenant_id=tenant_id,
                action_id=action_id,
                attempt_number=1,
                idempotency_key=f"{tenant_id}:{action_id}:{context['snapshot_hash']}",
                state="STARTED",
                executor_id=EXECUTOR_ID,
                fencing_token=lease.fencing_token,
                started_at=now,
                result={},
            )
        )
        action.status = "EXECUTING"
        action.updated_at = now
        action.version += 1
    try:
        if action_type == "restart_cache":
            adapter.restart_redis()
            verified: LabReadiness | OrderReadiness | RollbackReadiness = adapter.verify_redis()
        elif action_type == "restart_service":
            adapter.restart_order()
            verified = adapter.verify_order()
        else:
            backup_name = rollback_adapter.rollback(context["snapshot"], action_id)
            verified = rollback_adapter.verify(context["snapshot"]).model_copy(
                update={"backup_container": backup_name}
            )
    except (RollbackPreconditionFailed, RestartPreconditionFailed) as exc:
        _finish(
            settings,
            tenant_id,
            action_id,
            attempt_id,
            status="PRECONDITION_FAILED",
            reason=str(exc),
        )
        return "PRECONDITION_FAILED"
    except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
        _finish(
            settings,
            tenant_id,
            action_id,
            attempt_id,
            status="RECONCILIATION_REQUIRED",
            reason=f"Docker {action_type} result uncertain: {type(exc).__name__}",
        )
        return "RECONCILIATION_REQUIRED"
    if not verified.passed:
        _finish(
            settings,
            tenant_id,
            action_id,
            attempt_id,
            status="RECONCILIATION_REQUIRED",
            reason=f"Docker {action_type} returned success, but readiness did not recover",
            verification=verified,
        )
        return "RECONCILIATION_REQUIRED"
    _finish(
        settings,
        tenant_id,
        action_id,
        attempt_id,
        status="SUCCEEDED",
        reason=f"Docker {action_type} succeeded; readiness recovered",
        verification=verified,
    )
    return "SUCCEEDED"


def execute_authorized_redis(
    settings: Settings, tenant_id: UUID, action_id: UUID, adapter: LabDockerAdapter
) -> str:
    """Compatibility entry point for existing Redis Lab acceptance scripts."""
    context = _execution_context(settings, tenant_id, action_id)
    if context is None or context["snapshot"].action_type != "restart_cache":
        return "SKIPPED_NOT_AUTHORIZED"
    return execute_authorized_lab(settings, tenant_id, action_id, adapter)


def reconcile_no_effect(
    settings: Settings,
    tenant_id: UUID,
    action_id: UUID,
    adapter: LabDockerAdapter,
    *,
    operator_id: str,
    reason: str,
) -> str:
    """Only release an uncertain lease when Docker proves no restart occurred afterward."""
    if settings.profile != Profile.DEV:
        raise ValueError("Lab reconciliation requires the dev profile")
    if not operator_id.strip():
        raise ValueError("reconciliation operator is required")
    if len(reason.strip()) < 3:
        raise ValueError("reconciliation reason is required")
    with session_for(settings.database_url) as session:
        action = session.scalar(
            select(ActionRecord).where(
                ActionRecord.tenant_id == tenant_id, ActionRecord.id == action_id
            )
        )
        attempt = session.scalar(
            select(ActionAttemptRecord).where(
                ActionAttemptRecord.tenant_id == tenant_id,
                ActionAttemptRecord.action_id == action_id,
                ActionAttemptRecord.state == "RECONCILIATION_REQUIRED",
            )
        )
        if action is None or attempt is None or action.status != "RECONCILIATION_REQUIRED":
            return "NOT_UNCERTAIN"
        action_type = ActionSnapshot.model_validate(action.snapshot).action_type
        snapshot = ActionSnapshot.model_validate(action.snapshot)
        started_at = attempt.started_at
        attempt_id = attempt.id
    if action_type == "restart_cache":
        docker_started_at = adapter.redis_started_at()
    elif action_type == "restart_service":
        docker_started_at = adapter.order_started_at()
    elif action_type == "rollback_service":
        try:
            info = LabOrderRollbackAdapter(socket_path=adapter.socket_path).inspect_order()
            if fingerprint_from_inspect(info) != _expected(snapshot, "current"):
                return "AMBIGUOUS_DEPLOYMENT_OBSERVED"
            if info["State"].get("Running") is not True:
                return "CONTAINER_NOT_RUNNING"
            docker_started_at = datetime.fromisoformat(
                info["State"]["StartedAt"].replace("Z", "+00:00")
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return "CONTAINER_NOT_AVAILABLE"
    else:
        return "UNSUPPORTED_ACTION"
    if docker_started_at >= started_at:
        return "AMBIGUOUS_RESTART_OBSERVED"
    outcome = _finish(
        settings,
        tenant_id,
        action_id,
        attempt_id,
        status="FAILED",
        reason=(
            f"No {action_type} after attempt; Docker StartedAt={docker_started_at.isoformat()}; "
            f"operator={operator_id}; reason={reason.strip()}"
        ),
        expected_status="RECONCILIATION_REQUIRED",
    )
    return "FAILED_NO_EFFECT" if outcome else "STATE_CHANGED"


def reconcile_recovery_observed(
    settings: Settings,
    tenant_id: UUID,
    action_id: UUID,
    adapter: LabDockerAdapter,
    *,
    operator_id: str,
    reason: str,
) -> str:
    """Release isolation after observed recovery without attributing the restart to us."""
    if settings.profile != Profile.DEV:
        raise ValueError("Lab reconciliation requires the dev profile")
    if not operator_id.strip():
        raise ValueError("reconciliation operator is required")
    if len(reason.strip()) < 3:
        raise ValueError("reconciliation reason is required")
    with session_for(settings.database_url) as session:
        action = session.scalar(
            select(ActionRecord).where(
                ActionRecord.tenant_id == tenant_id, ActionRecord.id == action_id
            )
        )
        attempt = session.scalar(
            select(ActionAttemptRecord).where(
                ActionAttemptRecord.tenant_id == tenant_id,
                ActionAttemptRecord.action_id == action_id,
                ActionAttemptRecord.state == "RECONCILIATION_REQUIRED",
            )
        )
        if action is None or attempt is None or action.status != "RECONCILIATION_REQUIRED":
            return "NOT_UNCERTAIN"
        action_type = ActionSnapshot.model_validate(action.snapshot).action_type
        snapshot = ActionSnapshot.model_validate(action.snapshot)
        started_at = attempt.started_at
        attempt_id = attempt.id
    if action_type == "restart_cache":
        docker_started_at = adapter.redis_started_at()
    elif action_type == "restart_service":
        docker_started_at = adapter.order_started_at()
    elif action_type == "rollback_service":
        try:
            info = LabOrderRollbackAdapter(socket_path=adapter.socket_path).inspect_order()
            if fingerprint_from_inspect(info) != _expected(snapshot, "target"):
                return "TARGET_DEPLOYMENT_NOT_OBSERVED"
            docker_started_at = datetime.fromisoformat(
                info["State"]["StartedAt"].replace("Z", "+00:00")
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return "CONTAINER_NOT_AVAILABLE"
    else:
        return "UNSUPPORTED_ACTION"
    if docker_started_at < started_at:
        return "NO_RESTART_OBSERVED"
    if action_type == "restart_cache":
        verification: LabReadiness | OrderReadiness | RollbackReadiness = adapter.verify_redis()
    elif action_type == "restart_service":
        verification = adapter.verify_order()
    else:
        verification = LabOrderRollbackAdapter(socket_path=adapter.socket_path).verify(snapshot)
    if not verification.passed:
        return "RECOVERY_NOT_VERIFIED"
    outcome = _finish(
        settings,
        tenant_id,
        action_id,
        attempt_id,
        status="RECOVERY_OBSERVED",
        reason=(
            f"{action_type} observed after uncertain attempt; attribution unknown; "
            f"Docker StartedAt={docker_started_at.isoformat()}; "
            f"operator={operator_id.strip()}; reason={reason.strip()}"
        ),
        expected_status="RECONCILIATION_REQUIRED",
        verification=verification,
    )
    return "RECOVERY_OBSERVED" if outcome else "STATE_CHANGED"
