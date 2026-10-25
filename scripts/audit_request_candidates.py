"""Review persisted real windows, with source promotion and read-only candidate auditing."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select

from aegisops.application.partition_evaluation import evaluate_partition
from aegisops.application.request_signal_alerts import membership_evidence
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    AlertRecord,
    IncidentAlertRecord,
    RequestDerivedSignalRecord,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-start", action="append", required=True)
    args = parser.parse_args()
    starts = [datetime.fromisoformat(value.replace("Z", "+00:00"))
              for value in args.window_start]
    settings = Settings()
    require_dev_runtime(settings)
    tenant = settings.dev_tenant_id
    with session_for(settings.database_url) as session:
        signals = session.scalars(select(RequestDerivedSignalRecord).where(
            RequestDerivedSignalRecord.tenant_id == tenant,
            RequestDerivedSignalRecord.window_start.in_(starts),
        ).order_by(RequestDerivedSignalRecord.window_start,
                   RequestDerivedSignalRecord.service_name,
                   RequestDerivedSignalRecord.failure_code)).all()
    assert signals, "no persisted source signals"
    auth = {"Authorization": f"Bearer {settings.alert_webhook_token or 'aegisops_dev_only'}"}
    operator_token = settings.operator_token or "aegisops_operator_dev_only"
    operator = {"Authorization": f"Bearer {operator_token}"}
    alerts = {}
    checks = {}
    observations = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=20,
                      trust_env=False) as client:
        for signal in signals:
            path = f"/api/v1/request-failure-signals/{signal.id}/promote"
            response = client.post(path, headers=operator,
                json={"reason": "历史真实窗口回放：核对来源绑定请求关系、覆盖不足和混合症状"})
            response.raise_for_status()
            alerts[signal.id] = UUID(response.json()["alert_id"])
        deadline = time.monotonic() + 40
        incidents: dict[UUID, UUID] = {}
        while time.monotonic() < deadline:
            with session_for(settings.database_url) as session:
                incidents = {alert: incident for alert, incident in session.execute(
                    select(IncidentAlertRecord.alert_id,
                    IncidentAlertRecord.incident_id).where(
                        IncidentAlertRecord.tenant_id == tenant,
                        IncidentAlertRecord.alert_id.in_(alerts.values()),
                    )).all()}
            if len(incidents) == len(alerts):
                break
            time.sleep(0.5)
        checks["all_source_alerts_correlated"] = len(incidents) == len(alerts)
        assert checks["all_source_alerts_correlated"]
        checks["unauthorized_rejected"] = True
        for incident in sorted(set(incidents.values()), key=str):
            path = f"/api/v1/incidents/{incident}/request-candidates"
            checks["unauthorized_rejected"] &= client.get(path).status_code == 401
            response = client.get(path, headers=auth)
            response.raise_for_status()
            observations.append(response.json())
        checks["missing_incident_404"] = client.get(
            "/api/v1/incidents/ffffffff-ffff-ffff-ffff-ffffffffffff/request-candidates",
            headers=auth).status_code == 404
        replay = client.get(
            f"/api/v1/incidents/{observations[0]['incident_id']}/request-candidates",
            headers=auth)
        replay.raise_for_status()
        checks["candidate_replay_consistent"] = (
            replay.json()["candidates"] == observations[0]["candidates"])
        with session_for(settings.database_url) as session:
            after = session.execute(select(IncidentAlertRecord.alert_id,
                IncidentAlertRecord.incident_id).where(
                    IncidentAlertRecord.tenant_id == tenant,
                    IncidentAlertRecord.alert_id.in_(alerts.values()),
                )).all()
        before_groups: dict[UUID, list[str]] = {}
        after_groups: dict[UUID, list[str]] = {}
        for alert, incident in incidents.items():
            before_groups.setdefault(incident, []).append(str(alert))
        for alert, incident in after:
            after_groups.setdefault(incident, []).append(str(alert))
        checks["reading_candidates_preserves_incident_partition"] = (
            evaluate_partition(list(before_groups.values()),
                               list(after_groups.values()))["exact_match"])
        with session_for(settings.database_url) as session:
            unbound = session.scalar(select(IncidentAlertRecord.incident_id)
                .join(AlertRecord, (AlertRecord.tenant_id == IncidentAlertRecord.tenant_id)
                    & (AlertRecord.id == IncidentAlertRecord.alert_id))
                .where(AlertRecord.tenant_id == tenant, AlertRecord.source != "request-detector",
                       ~IncidentAlertRecord.incident_id.in_(incidents.values()))
                .order_by(IncidentAlertRecord.incident_id).limit(1))
        if unbound is not None:
            response = client.get(f"/api/v1/incidents/{unbound}/request-candidates", headers=auth)
            response.raise_for_status()
            unbound_observation = response.json()
            checks["prometheus_without_source_is_unbound"] = (
                unbound_observation["status"] == "NO_DIRECT_SOURCE"
                and not unbound_observation["candidates"])
        else:
            unbound_observation = None
            checks["prometheus_without_source_is_unbound"] = False
    all_candidates = [c for row in observations for c in row["candidates"]]
    checks["has_verified_candidates"] = bool(all_candidates)
    checks["no_automatic_merge_or_root_claim"] = all(
        not c["automatic_merge_allowed"] and c["root_cause_status"] == "UNDETERMINED"
        and c["current_fault_status"] == "UNKNOWN" for c in all_candidates)
    checks["both_proof_ends_are_direct_members"] = True
    for candidate in all_candidates:
        own = membership_evidence(settings, tenant_id=tenant,
            alert_id=UUID(candidate["source_alert_id"]))[0].payload["member_event_ids"]
        other = membership_evidence(settings, tenant_id=tenant,
            alert_id=UUID(candidate["other_alert_id"]))[0].payload["member_event_ids"]
        for proof in candidate["proofs"]:
            a, b = ((proof["upstream_event_id"], proof["downstream_event_id"])
                    if candidate["direction"] == "DOWNSTREAM" else
                    (proof["downstream_event_id"], proof["upstream_event_id"]))
            checks["both_proof_ends_are_direct_members"] &= a in own and b in other
    checks["partial_cohort_coverage_is_visible"] = any(
        "SOURCE_MEMBERS_UNCOVERED" in c["conflicting_features"]
        or "OTHER_MEMBERS_UNCOVERED" in c["conflicting_features"] for c in all_candidates)
    checks["mixed_source_groups_are_ambiguous"] = any(
        "MULTIPLE_PEER_SOURCE_GROUPS" in c["conflicting_features"] for c in all_candidates)
    checks["same_gateway_symptom_has_transport_and_http_peers"] = any(
        {c["other_failure_code"] for c in row["candidates"]
         if c["source_failure_code"] == "upstream_http_5xx"
         and c["direction"] == "DOWNSTREAM"}
        == {"dependency_transport", "dependency_http_5xx"} for row in observations)
    checks["complete_business_path_still_requires_review"] = any(
        c["decision"] == "REVIEW_REQUEST_PATH" and not c["automatic_merge_allowed"]
        for c in all_candidates)
    checks["budget_limits_are_visible"] = any(
        "超出16条" in limitation for row in observations for limitation in row["limitations"])
    checks["no_stale_observations"] = all(row["status"] != "STALE" for row in observations)
    pairs = {(p["trace_id"], p["upstream_event_id"], p["downstream_event_id"])
             for c in all_candidates for p in c["proofs"]}
    result = {"captured_at": datetime.now(UTC).isoformat(), "passed": all(checks.values()),
        "windows": args.window_start, "checks": checks,
        "source_signals": [{"signal_id": str(s.id), "service": s.service_name,
            "failure_code": s.failure_code, "window_start": s.window_start.isoformat(),
            "member_count": s.event_count, "alert_id": str(alerts[s.id]),
            "incident_id": str(incidents[alerts[s.id]])} for s in signals],
        "candidate_count": len(all_candidates), "distinct_event_pair_count": len(pairs),
        "observations": observations, "unbound_observation": unbound_observation,
        "interpretation": "既有真实窗口结构回放，不是新盲测；未改事故分组，未评判共同根因。"}
    report_path = Path("eval") / f"request-candidates-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "passed": result["passed"],
        "checks": checks, "candidate_count": len(all_candidates),
        "distinct_event_pair_count": len(pairs)}, ensure_ascii=False))
    if not result["passed"]:
        raise AssertionError("request candidate audit failed; see saved evidence")


if __name__ == "__main__":
    main()
