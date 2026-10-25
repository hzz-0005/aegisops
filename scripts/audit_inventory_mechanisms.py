"""Check what the saved investigations actually observed in each new inventory variant."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

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
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    settings = Settings()
    require_dev_runtime(settings)
    results = []
    with session_for(settings.database_url) as session:
        for entry in manifest["samples"]:
            source = json.loads((root/entry["source_report"]).read_text(encoding="utf-8"))
            case = next(s for s in source if s["fault"] == entry["fault"]
                        and s["repeat_index"] == entry["source_repeat"])
            start = datetime.fromisoformat(case["started_at"])
            end = max(datetime.fromisoformat(a["received_at"]) for a in case["alerts"])
            ids = {UUID(a["incident_id"]) for a in case["alerts"] if a["incident_id"]}
            rows = session.execute(select(Report, Evidence)
                .join(Evidence, (Evidence.report_id == Report.id)
                      & (Evidence.tenant_id == Report.tenant_id))
                .where(Report.tenant_id == settings.dev_tenant_id,
                       Report.incident_id.in_(ids), Report.completed_at >= start,
                       Report.completed_at <= end + timedelta(seconds=30),
                       Evidence.observed_at >= start, Evidence.observed_at <= end,
                       Evidence.kind.in_(["RUNTIME_STATE_OBSERVATION",
                                          "VERIFIED_DEPENDENCY_REQUEST"]))).all()
            facts = [{"incident_id": str(r.incident_id), "evidence_id": str(e.id),
                      "kind": e.kind, "polarity": e.polarity, "payload": e.payload,
                      "observed_at": e.observed_at.isoformat()} for r, e in rows
                     if e.observed_at is not None]
            runtime = [f for f in facts if f["kind"] == "RUNTIME_STATE_OBSERVATION"
                       and f["payload"].get("service") == "inventory-service"]
            dependency = [f for f in facts if f["kind"] == "VERIFIED_DEPENDENCY_REQUEST"
                          and f["payload"].get("dependency") == "inventory-service"]
            results.append({"sample_id": entry["sample_id"], "facts": runtime + dependency,
                "observed_states": sorted({f["payload"].get("state") for f in runtime}),
                "observed_request_failures": sorted({f["payload"].get("failure_code")
                                                      for f in dependency}),
                "interpretation": ("运行状态与请求失败是上下文；运行中超时不能证明数据库表锁，"
                                   "退出也不证明整个事故只有一个根因。")})
    path = root/"eval"/f"inventory-mechanisms-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps({"samples": results}, ensure_ascii=False, indent=2),encoding="utf-8")
    print(json.dumps({"samples": [{k: v for k, v in s.items() if k != "facts"}
                                   for s in results], "report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
