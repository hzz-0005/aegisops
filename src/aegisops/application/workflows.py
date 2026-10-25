"""Deterministic Temporal workflow definitions; external I/O lives in activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class IncidentRefreshWorkflow:
    """A bounded read-only refresh, separate from the existing operator wait history."""

    @workflow.run
    async def run(self, tenant_id: str, incident_id: str) -> str:
        return await workflow.execute_activity(
            "investigate_incident", args=[tenant_id, incident_id],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
        )


@workflow.defn
class IncidentWorkflow:
    def __init__(self) -> None:
        self._decisions: list[tuple[str, int, str, str]] = []
        self._review_state = "STARTING"

    @workflow.signal(name="operator_decision")
    def operator_decision(
        self, decision: str, expected_version: int, reason: str, operator_id: str
    ) -> None:
        self._decisions.append((decision, expected_version, reason, operator_id))

    @workflow.query(name="review_state")
    def review_state(self) -> str:
        return self._review_state

    @workflow.run
    async def run(
        self, tenant_id: str, incident_id: str, review_timeout_seconds: int = 86400
    ) -> str:
        result = await workflow.execute_activity(
            "enter_investigation",
            args=[tenant_id, incident_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
        if result != "INVESTIGATING":
            return result
        self._review_state = "INVESTIGATING_READ_ONLY"
        try:
            investigation = await workflow.execute_activity(
                "investigate_incident",
                args=[tenant_id, incident_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2), maximum_attempts=3
                ),
            )
        except ActivityError:
            investigation = "INVESTIGATION_FAILED"
        if investigation == "SKIPPED_MISSING":
            return investigation
        deadline = workflow.now() + timedelta(seconds=review_timeout_seconds)
        self._review_state = (
            "WAITING_OPERATOR:REPORT_READY"
            if investigation == "SUPPORTED"
            else f"WAITING_OPERATOR:{investigation}"
        )
        while True:
            remaining = deadline - workflow.now()
            if remaining.total_seconds() <= 0:
                break
            try:
                await workflow.wait_condition(
                    lambda: bool(self._decisions), timeout=remaining
                )
            except TimeoutError:
                break
            decision, expected_version, reason, operator_id = self._decisions.pop(0)
            self._review_state = "APPLYING_DECISION"
            outcome = await workflow.execute_activity(
                "apply_operator_decision",
                args=[
                    tenant_id,
                    incident_id,
                    decision,
                    expected_version,
                    reason,
                    operator_id,
                ],
                start_to_close_timeout=timedelta(seconds=30),
            )
            if outcome in {"CANCELLED", "ESCALATED", "RESOLVED", "SKIPPED_TERMINAL"}:
                return outcome
            self._review_state = f"WAITING_OPERATOR:{outcome}"
        self._review_state = "REVIEW_TIMED_OUT"
        return await workflow.execute_activity(
            "expire_operator_wait",
            args=[tenant_id, incident_id, review_timeout_seconds],
            start_to_close_timeout=timedelta(seconds=30),
        )
