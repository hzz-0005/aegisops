"""Replay archived verified proofs against persisted direct sources without changing incidents."""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from aegisops.application.investigation.request_candidates import Anchor
from aegisops.application.investigation.request_shadow import shadow_partition
from aegisops.application.partition_evaluation import evaluate_partition
from aegisops.application.request_events import _view
from aegisops.application.request_failure_links import RequestFailureLink
from aegisops.application.request_signal_alerts import membership_evidence
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import IncidentAlertRecord, RequestFailureEventRecord

ROOT = Path(__file__).resolve().parents[1]


def load(relative: str, provenance: dict) -> dict:
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("replay input must stay within project")
    data = path.read_bytes()
    provenance[relative] = hashlib.sha256(data).hexdigest()
    return json.loads(data)


def projected_metrics(expected: list[list[str]], groups: list[dict],
                      scored: set[str]) -> dict:
    actual = [sorted(set(g["members"]) & scored) for g in groups]
    return evaluate_partition(expected, [g for g in actual if g])


def replay_case(settings: Settings, entry: dict) -> dict:
    provenance: dict[str, str] = {}
    tenant = settings.dev_tenant_id
    proof_pairs = []
    if "candidate_report" in entry:
        report = load(entry["candidate_report"], provenance)
        if not report.get("passed") or any(r["status"] == "STALE"
                                          for r in report["observations"]):
            raise ValueError("historical candidate snapshot was not fresh when recorded")
        meta = report["source_signals"]
        raw_proofs = []
        for observation in report["observations"]:
            for candidate in observation["candidates"]:
                for proof in candidate["proofs"]:
                    own, peer = ((proof["upstream_event_id"], proof["downstream_event_id"])
                        if candidate["direction"] == "DOWNSTREAM" else
                        (proof["downstream_event_id"], proof["upstream_event_id"]))
                    proof_pairs.append((candidate["source_alert_id"], own,
                                        candidate["other_alert_id"], peer))
                    raw_proofs.append(proof)
    else:
        report = load(entry["membership_report"], provenance)
        if not report.get("passed"):
            raise ValueError("source promotion audit did not pass")
        meta = report["results"]
        proof_report = load(entry["proof_report"], provenance)
        raw_proofs = [sample["link"] for sample in proof_report["real_samples"]]
    anchors = []
    for item in meta:
        alert = UUID(item["alert_id"])
        evidence = membership_evidence(settings, tenant_id=tenant, alert_id=alert)
        if len(evidence) != 1:
            raise ValueError("replay requires verified direct source membership")
        payload = evidence[0].payload
        expected_count = item.get("member_count", item.get("exact_member_count"))
        if payload["signal_id"] != item["signal_id"] or payload["event_count"] != expected_count:
            raise ValueError("recorded source snapshot no longer matches persisted membership")
        anchors.append(Anchor(tenant, UUID(item["incident_id"]), 0, alert,
            UUID(payload["signal_id"]), frozenset(UUID(v) for v in payload["member_event_ids"]),
            payload["service"], payload["failure_code"]))
    by_alert = {str(anchor.alert_id): anchor for anchor in anchors}
    for own_alert, own, peer_alert, peer in proof_pairs:
        if (own_alert not in by_alert or peer_alert not in by_alert
                or UUID(own) not in by_alert[own_alert].members
                or UUID(peer) not in by_alert[peer_alert].members):
            raise ValueError("archived candidate proof is not anchored on both ends")
    member_ids = set().union(*(a.members for a in anchors))
    with session_for(settings.database_url) as session:
        records = session.scalars(select(RequestFailureEventRecord).where(
            RequestFailureEventRecord.tenant_id == tenant,
            RequestFailureEventRecord.id.in_(member_ids),
        ).order_by(RequestFailureEventRecord.id)).all()
        events = [_view(record, "PERSISTED_SOURCE_MEMBER") for record in records]
        before = sorted((str(a), str(i)) for a, i in session.execute(
            select(IncidentAlertRecord.alert_id, IncidentAlertRecord.incident_id).where(
                IncidentAlertRecord.tenant_id == tenant,
                IncidentAlertRecord.alert_id.in_([a.alert_id for a in anchors]))).all())
    proofs = [RequestFailureLink.model_validate(raw) for raw in raw_proofs]
    result = shadow_partition(tenant_id=tenant, anchors=anchors, events=events, proofs=proofs)
    shuffled = shadow_partition(tenant_id=tenant, anchors=list(reversed(anchors)),
        events=list(reversed(events)), proofs=list(reversed(proofs)) + proofs)
    checks = {"reordered_duplicate_replay_consistent": result == shuffled,
              "complete_member_universe_preserved": {v for g in result["groups"]
                  for v in g["members"]} == set(map(str, member_ids)),
              "archived_proof_identity_matches": result["rejected_proof_count"] == 0,
              "no_root_or_automatic_merge_claim": not result["automatic_merge_allowed"]
                  and result["root_cause_status"] == "UNDETERMINED"}
    oracle_metrics = None
    if "oracle_report" in entry:
        oracle = load(entry["oracle_report"], provenance)
        known_events = oracle["events"]
        facts = {str(e.id): e for e in events}
        trace_roots = {}
        for root, traces in oracle["traces_by_injected_root"].items():
            for trace in traces:
                if trace in trace_roots:
                    raise ValueError("independent trace oracle is ambiguous")
                trace_roots[trace] = root
        request_groups: dict[str, list[str]] = defaultdict(list)
        root_groups: dict[str, list[str]] = defaultdict(list)
        for event in known_events:
            fact = facts.get(event["id"])
            if (fact is None or event["trace_id"] not in trace_roots
                    or event["trace_id"] != fact.trace_id or event["service"] != fact.service
                    or event["failure_code"] != fact.failure_code
                    or event["resource"] != fact.resource
                    or datetime.fromisoformat(event["occurred_at"]) != fact.occurred_at):
                raise ValueError("independent oracle does not match persisted source event")
            request_groups[event["trace_id"]].append(event["id"])
            root_groups[trace_roots[event["trace_id"]]].append(event["id"])
        scored = {event["id"] for event in known_events}
        projected = [{"members": sorted(map(str, a.members))} for a in anchors]
        ownership = Counter(v for a in anchors for v in map(str, a.members) if v in scored)
        baseline = (projected_metrics(list(root_groups.values()), projected, scored)
                    if all(count == 1 for count in ownership.values()) else None)
        oracle_metrics = {"basis": entry["oracle_basis"], "known_member_count": len(scored),
            "total_source_member_count": len(member_ids),
            "oracle_coverage": len(scored) / len(member_ids) if member_ids else None,
            "unscored_member_ids": sorted(set(map(str, member_ids)) - scored),
            "request_propagation_partition": projected_metrics(
                list(request_groups.values()), result["groups"], scored),
            "root_partition_if_propagation_were_misused": projected_metrics(
                list(root_groups.values()), result["groups"], scored),
            "source_cohort_partition_against_root_truth": baseline,
            "source_cohort_baseline_limitation": (
                None if baseline else "来源成员重叠，来源组不是合法Partition，不能强行评分。"),
            "interpretation": "客户端串行请求真值只适用于本样本；根因标签仅进入评价，"
                              "未进入影子算法。已知子集成功不能算全部成员成功。"}
        checks["known_serial_request_partition_matches"] = oracle_metrics[
            "request_propagation_partition"]["exact_match"]
    with session_for(settings.database_url) as session:
        after = sorted((str(a), str(i)) for a, i in session.execute(
            select(IncidentAlertRecord.alert_id, IncidentAlertRecord.incident_id).where(
                IncidentAlertRecord.tenant_id == tenant,
                IncidentAlertRecord.alert_id.in_([a.alert_id for a in anchors]))).all())
    checks["persisted_alert_ownership_unchanged"] = before == after
    return {"case_id": entry["case_id"], "passed": all(checks.values()), "checks": checks,
        "provenance_sha256": provenance, "shadow": result, "oracle_metrics": oracle_metrics,
        "proof_basis": "历史已核验父Span证明与当前持久化来源身份一致；未重新查询Tempo，"
                       "不是鲜活调查或新盲测，不改变事故。"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    settings = Settings()
    require_dev_runtime(settings)
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    cases = [replay_case(settings, entry) for entry in manifest["cases"]]
    report = {"captured_at": datetime.now(UTC).isoformat(), "manifest": str(args.manifest),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "scope": manifest["evaluation_scope"], "passed": all(c["passed"] for c in cases),
        "cases": cases, "automatic_correlation_decision": "HOLD",
        "reason": "单次传播路径不足以跨请求认定共同根因；原始Prom告警尚无直接来源成员。"}
    path = ROOT / "eval" / f"request-shadow-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "passed": report["passed"],
        "cases": [{"case_id": c["case_id"], "passed": c["passed"],
                   "members": c["shadow"]["member_count"],
                   "joined": c["shadow"]["joined_member_count"],
                   "unresolved": c["shadow"]["unresolved_member_count"],
                   "oracle_member_count": (c["oracle_metrics"]["known_member_count"]
                                           if c["oracle_metrics"] else None),
                   "request_partition_match": (c["oracle_metrics"][
                       "request_propagation_partition"]["exact_match"]
                       if c["oracle_metrics"] else None)} for c in cases]}, ensure_ascii=False))
    if not report["passed"]:
        raise AssertionError("shadow replay failed; inspect saved evidence")


if __name__ == "__main__":
    main()
