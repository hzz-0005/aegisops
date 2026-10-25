"""Deploy a faulty Order revision, correlate two real alerts, then roll it back."""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "lab" / "scenarios" / "bad_deployment.compose.yaml"
ORDER = "http://127.0.0.1:8003"
GATEWAY = "http://127.0.0.1:8001"
API = "http://127.0.0.1:8000/api/v1"
DATABASE_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
OPERATOR_TOKEN = os.getenv("AEGISOPS_OPERATOR_TOKEN", "aegisops_operator_dev_only")


def deploy(change_id: str | None) -> None:
    files = ["-f", "compose.yaml"]
    env = os.environ.copy()
    if change_id:
        files += ["-f", str(OVERLAY)]
        env["LAB_DEPLOYMENT_CHANGE_ID"] = change_id
    subprocess.run(
        ["docker", "compose", *files, "up", "-d", "--no-deps", "--force-recreate", "order-service"],
        cwd=ROOT,
        env=env,
        check=True,
    )


def deployment_fingerprint() -> dict[str, str | bool]:
    def inspect(field: str) -> str:
        return subprocess.run(
            ["docker", "inspect", "--format", field, "aegisops-order-service-1"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    env = dict(
        item.split("=", 1)
        for item in json.loads(inspect("{{json .Config.Env}}"))
        if "=" in item
    )
    return {
        "image_id": inspect("{{.Image}}"),
        "compose_config_hash": inspect(
            '{{index .Config.Labels "com.docker.compose.config-hash"}}'
        ),
        "change_id": env["LAB_DEPLOYMENT_CHANGE_ID"],
        "bad_deployment": env["LAB_BAD_DEPLOYMENT"] == "true",
    }


def await_revision(client: httpx.Client, change_id: str, bad: bool) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{ORDER}/deployment")
            if response.status_code == 200 and response.json() == {
                "change_id": change_id,
                "bad_deployment": bad,
            }:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Order deployment {change_id} did not become ready")


def checkout(client: httpx.Client) -> httpx.Response:
    return client.post(
        f"{GATEWAY}/checkout",
        json={"idempotency_key": str(uuid4()), "sku": "sku-demo", "quantity": 1},
    )


def await_metric_samples(client: httpx.Client, change_id: str) -> None:
    deadline = time.monotonic() + 35
    for metric in ("lab_order_deployment_failures_total", "lab_gateway_order_failures_total"):
        while time.monotonic() < deadline:
            response = client.get(
                "http://127.0.0.1:9090/api/v1/query",
                params={"query": f'{metric}{{change_id="{change_id}"}}'},
            )
            response.raise_for_status()
            if response.json()["data"]["result"]:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Prometheus did not scrape {metric} for {change_id}")


def await_incident(change_id: str, since: datetime) -> str:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with psycopg.connect(DATABASE_URL) as connection:
            rows = connection.execute(
                """
                SELECT a.payload, ia.incident_id
                FROM alerts a JOIN incident_alerts ia ON ia.alert_id = a.id
                WHERE a.received_at >= %s
                  AND a.service_name = ANY(%s)
                """,
                (since, ["order-service", "lab-gateway"]),
            ).fetchall()
        selected = {
            row[0].get("alertname"): row
            for row in rows
            if row[0].get("alertname") in {"OrderBadDeployment", "GatewayOrderFailures"}
        }
        if len(selected) == 2:
            order = selected["OrderBadDeployment"]
            gateway = selected["GatewayOrderFailures"]
            if order[0].get("change_id") != change_id or gateway[0].get("change_id") != change_id:
                raise RuntimeError("deployment or gateway alert lost its actual change ID")
            if order[1] != gateway[1]:
                raise RuntimeError("order and gateway alerts formed separate Incidents")
            with psycopg.connect(DATABASE_URL) as connection:
                members = connection.execute(
                    "SELECT count(*) FROM incident_alerts WHERE incident_id = %s",
                    (order[1],),
                ).fetchone()
            if members is None or members[0] != 2:
                raise RuntimeError("bad deployment was merged with an older unrelated Incident")
            return str(order[1])
        time.sleep(1)
    raise RuntimeError("actual deployment alerts did not reach a common Incident")


def main() -> None:
    change_id = f"lab-order-bad-{uuid4().hex[:12]}"
    with httpx.Client(timeout=10) as client:
        baseline = checkout(client)
        if baseline.status_code != 200:
            raise RuntimeError(f"baseline checkout failed: {baseline.status_code}")
        before = deployment_fingerprint()
        if before["bad_deployment"]:
            raise RuntimeError("Order baseline is not a healthy deployment")
        started = datetime.now(UTC)
        deploy(change_id)
        try:
            await_revision(client, change_id, True)
            after = deployment_fingerprint()
            if after["change_id"] != change_id or not after["bad_deployment"]:
                raise RuntimeError("faulty deployment fingerprint differs from running Order")
            change = client.post(
                f"{API}/change-events",
                headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
                json={
                    "source": "lab-deployment",
                    "external_id": change_id,
                    "service": "order-service",
                    "environment": "lab",
                    "change_type": "DEPLOYMENT",
                    "from_ref": before["change_id"],
                    "to_ref": change_id,
                    "deployment": {"before": before, "after": after},
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            if change.status_code != 201:
                raise RuntimeError(f"actual deployment change was not recorded: {change.text}")
            for _ in range(2):
                failed = checkout(client)
                if failed.status_code != 500:
                    raise RuntimeError(
                        f"bad deployment did not break checkout: {failed.status_code}"
                    )
            await_metric_samples(client, change_id)
            for _ in range(2):
                failed = checkout(client)
                if failed.status_code != 500:
                    raise RuntimeError("second bad deployment request did not fail")
            incident_id = await_incident(change_id, started)
        finally:
            deploy(None)
            await_revision(client, "baseline", False)
        recovered = checkout(client)
        if recovered.status_code != 200:
            raise RuntimeError(f"checkout did not recover after rollback: {recovered.status_code}")
    print(
        f"bad deployment, change event, two alerts, Incident, and rollback verified: {incident_id}"
    )


if __name__ == "__main__":
    main()
