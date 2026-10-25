"""Read current Lab health and selected Docker metadata without printing environment secrets."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

from aegisops.config import Settings, require_dev_runtime
from scripts.evaluate_fault_battery import BUSINESS_DB_URL


def main() -> None:
    settings = Settings()
    require_dev_runtime(settings)
    identities = subprocess.check_output(
        ["docker", "ps", "-q"], encoding="utf-8", timeout=15).split()
    inspected = json.loads(subprocess.check_output(
        ["docker", "inspect", *identities], encoding="utf-8", timeout=15))
    containers = []
    routes = {}
    for item in inspected:
        labels = item["Config"].get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        containers.append({"name": item["Name"].lstrip("/"), "project": project,
                           "service": service, "state": item["State"]["Status"],
                           "paused": item["State"].get("Paused", False)})
        if project == "aegisops" and service in {"order-service", "lab-gateway"}:
            # Select only this public route; never write the complete environment.
            for entry in item["Config"].get("Env") or []:
                key, _, value = entry.partition("=")
                if key == "LAB_REQUEST_EVENT_URL":
                    routes[service] = value
    with httpx.Client(timeout=5, trust_env=False) as client:
        api = client.get("http://127.0.0.1:8000/api/v1/health/ready").status_code
        ui = client.get("http://127.0.0.1:8010/").status_code
        alerts = client.get("http://127.0.0.1:9093/api/v2/alerts")
        alerts.raise_for_status()
        active = [a["labels"].get("alertname") for a in alerts.json()]
    with psycopg.connect(BUSINESS_DB_URL, connect_timeout=2) as db:
        waiting = db.execute("""SELECT count(*) FROM pg_locks l JOIN pg_class c
            ON c.oid=l.relation JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='lab' AND c.relname='inventory_items' AND NOT l.granted""").fetchone()
    assert waiting is not None
    passed = (api == ui == 200 and not active and waiting[0] == 0
              and all(c["project"] == "aegisops" and not c["paused"] for c in containers)
              and routes == {"order-service": "http://api:8000/api/v1/request-events",
                             "lab-gateway": "http://api:8000/api/v1/request-events"})
    result = {"captured_at": datetime.now(UTC).isoformat(), "passed": passed,
              "api_status": api, "ui_status": ui, "active_alerts": active,
              "inventory_waiting_locks": waiting[0], "request_event_routes": routes,
              "running_containers": containers,
              "interpretation": "当前现场健康不表示历史事故全部关闭或已具备生产接入能力。"}
    path = Path("eval") / f"lab-runtime-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path.resolve()), "passed": passed,
                      "running_container_count": len(containers), "api_status": api,
                      "ui_status": ui, "active_alert_count": len(active),
                      "inventory_waiting_locks": waiting[0]}, ensure_ascii=False))
    if not passed:
        raise AssertionError("Lab runtime audit failed")


if __name__ == "__main__":
    main()
