"""Audit real request ancestry and explicitly synthetic negative controls."""

import argparse
import base64
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from aegisops.application.request_failure_links import (
    RequestLinkUnverifiable,
    links_for_events,
    trace_events,
)
from aegisops.config import Settings, require_dev_runtime
from aegisops.infrastructure.database.engine import session_for
from aegisops.infrastructure.database.models import (
    InvestigationEvidenceRecord as Evidence,
)
from aegisops.infrastructure.database.models import (
    InvestigationReportRecord as Report,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("collision_report", type=Path)
    args = parser.parse_args()
    truth = json.loads(args.collision_report.read_text(encoding="utf-8"))
    settings = Settings()
    require_dev_runtime(settings)
    samples = []
    event_sets = []
    expected_pairs = set()
    for root, traces in truth["traces_by_injected_root"].items():
        for trace_id in traces:
            events = trace_events(settings, tenant_id=settings.dev_tenant_id, trace_id=trace_id)
            response = httpx.get(f"{settings.tempo_url}/api/traces/{trace_id}", timeout=15)
            response.raise_for_status()
            batches = response.json().get("batches", [])
            links = links_for_events(events, batches)
            expected = {row["id"] for row in truth["events"] if row["trace_id"] == trace_id}
            assert len(links) == 1 and len(expected) == 2
            link = links[0]
            pair = (str(link.upstream_event_id), str(link.downstream_event_id))
            assert set(pair) == expected
            assert (
                link.upstream_service == "lab-gateway"
                and link.downstream_service == "order-service"
            )
            code = (
                "business_contract_rejected" if root == "contract_drift" else "dependency_transport"
            )
            assert link.downstream_failure_code == code
            resource = "sku-demo-currency" if root == "contract_drift" else "payment-service"
            assert link.downstream_resource == resource
            expected_pairs.add(pair)
            severed = deepcopy(batches)
            boundary_id = base64.b64encode(bytes.fromhex(link.span_path[1])).decode()
            for batch in severed:
                for scope in batch.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        if span["spanId"] == boundary_id:
                            span["parentSpanId"] = base64.b64encode(
                                bytes.fromhex("f" * 16)
                            ).decode()
            assert links_for_events(events, severed) == []
            event_sets.append((events, batches))
            samples.append(
                {
                    "acceptance_root_label": root,
                    "link": link.model_dump(mode="json"),
                    "synthetic_severed_parent_rejected": True,
                }
            )
    try:
        links_for_events([event_sets[0][0][0], event_sets[-1][0][1]], event_sets[0][1])
    except RequestLinkUnverifiable:
        mixed_rejected = True
    else:
        mixed_rejected = False
    with session_for(settings.database_url) as session:
        rows = session.execute(
            select(Evidence, Report.incident_id)
            .join(
                Report, (Evidence.report_id == Report.id) & (Evidence.tenant_id == Report.tenant_id)
            )
            .where(
                Evidence.tenant_id == settings.dev_tenant_id,
                Report.graph_version == "readonly-investigation-v7-request-links",
                Evidence.kind == "VERIFIED_REQUEST_FAILURE_LINK",
            )
        ).all()
        persisted = []
        for evidence, incident_id in rows:
            payload = evidence.payload
            pair = (payload["upstream_event_id"], payload["downstream_event_id"])
            if pair in expected_pairs:
                assert evidence.polarity == "CONTEXT"
                assert payload["alert_binding_status"] == "UNBOUND"
                assert payload["root_cause_status"] == "UNDETERMINED"
                persisted.append({"pair": pair, "incident_id": str(incident_id)})
    persisted_pairs = {tuple(row["pair"]) for row in persisted}
    now = datetime.now(UTC)
    report = {
        "captured_at": now.isoformat(),
        "source_report": str(args.collision_report),
        "real_samples": samples,
        "real_request_pairs": len(expected_pairs),
        "persisted_distinct_pairs": len(persisted_pairs),
        "persisted_evidence": persisted,
        "synthetic_controls": {
            "severed_parent": len(samples),
            "mixed_trace_rejected": mixed_rejected,
        },
        "passed": persisted_pairs == expected_pairs and mixed_rejected,
        "scope": "请求结构关系；不验证根因、聚合告警绑定或修复效果。",
    }
    path = Path("eval") / f"request-links-audit-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "real_pairs": len(expected_pairs),
                "persisted_pairs": len(persisted_pairs),
                "report": str(path),
            },
            ensure_ascii=False,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
