"""Versioned, explainable alert-pair correlation rules."""

from dataclasses import dataclass

from aegisops.application.normalization import NormalizedAlert

RULE_VERSION = "deterministic-v9"
ATTACH_THRESHOLD = 0.50
AMBIGUITY_MARGIN = 0.10


def has_ambiguous_candidates(best_score: float, runner_up_score: float) -> bool:
    """Require a clear winner before attaching an alert to an existing Incident."""
    return runner_up_score >= ATTACH_THRESHOLD and best_score - runner_up_score <= AMBIGUITY_MARGIN


@dataclass(frozen=True)
class EvidenceScore:
    total_score: float
    matched_features: dict[str, float]
    conflicting_features: dict[str, float]

    @property
    def should_attach(self) -> bool:
        return self.total_score >= ATTACH_THRESHOLD


def evaluate_pair(
    incoming: NormalizedAlert,
    existing: NormalizedAlert,
    *,
    shared_dependency: bool = False,
) -> EvidenceScore:
    matched: dict[str, float] = {}
    conflicting: dict[str, float] = {}
    seconds_apart = abs((incoming.occurred_at - existing.occurred_at).total_seconds())
    if seconds_apart > 15 * 60:
        return EvidenceScore(0.0, {}, {"outside_time_window": -1.0})
    matched["within_60_seconds" if seconds_apart <= 60 else "within_15_minutes"] = (
        0.15 if seconds_apart <= 60 else 0.05
    )
    if incoming.resource_id and existing.resource_id:
        if incoming.resource_id == existing.resource_id:
            matched["same_resource"] = 0.25
        else:
            conflicting["different_resource"] = -0.30
    if incoming.change_id and existing.change_id:
        if incoming.change_id == existing.change_id:
            matched["same_change"] = 0.45
        else:
            conflicting["different_change"] = -0.50
    elif bool(incoming.change_id) != bool(existing.change_id):
        conflicting["missing_change_context"] = -0.45
    if (
        incoming.signal_type != existing.signal_type
        and incoming.signal_type != "unknown"
        and existing.signal_type != "unknown"
    ):
        # A shared deployment is context; conflicting domains still need independent evidence.
        conflicting["different_signal_type"] = -0.80
    if incoming.service_id == existing.service_id:
        matched["same_service"] = 0.20
    elif shared_dependency:
        matched["shared_dependency"] = 0.25
    else:
        conflicting["different_service_without_known_relation"] = -0.55
    if incoming.alert_name == existing.alert_name:
        matched["same_alert_type"] = 0.15
    elif (
        incoming.service_id == existing.service_id
        and not (incoming.change_id and incoming.change_id == existing.change_id)
    ):
        conflicting["different_alert_type_without_change"] = -0.15
    roles = {incoming.labels.get("signal_role"), existing.labels.get("signal_role")}
    if roles == {"symptom", "fault"}:
        fault = incoming if incoming.labels.get("signal_role") == "fault" else existing
        symptom = existing if fault is incoming else incoming
        if (
            shared_dependency and incoming.service_id != existing.service_id
            and incoming.change_id and incoming.change_id == existing.change_id
            and seconds_apart <= 60
            and fault.resource_id == symptom.resource_id == fault.service_id
        ):
            matched["dependent_symptom_context"] = 0.40
    total = round(sum(matched.values()) + sum(conflicting.values()), 2)
    return EvidenceScore(total, matched, conflicting)
