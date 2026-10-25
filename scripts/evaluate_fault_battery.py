"""Blind real-fault battery: onset-to-alert, partition, wrong-root and action risk."""

import argparse
import json
import os
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg

from aegisops.config import Settings, require_dev_runtime
from lab.scenarios.redis_down import compose

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.getenv(
    "AEGISOPS_LAB_DATABASE_URL",
    "postgresql://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
)
BUSINESS_DB_URL = os.getenv(
    "AEGISOPS_LAB_BUSINESS_DATABASE_URL",
    "postgresql://lab_orders:lab_orders_dev_only@127.0.0.1:5433/lab_orders",
)
CASES = (
    ("inventory_process_frozen", "inventory-service", "pause"),
    ("gateway_process_frozen", "lab-gateway", "pause"),
    ("payment_process_stopped", "payment-service", "stop"),
    ("payment_process_frozen", "payment-service", "pause"),
    ("order_process_stopped", "order-service", "stop"),
    ("business_database_stopped", "lab-orders-postgres", "stop"),
    ("gateway_process_stopped", "lab-gateway", "stop"),
    ("simulated_payment_provider_timeout", "payment-service", "provider-delay"),
    ("inventory_currency_drift", "inventory-service", "inventory-currency-drift"),
    ("payment_dns_target_invalid", "order-service", "payment-dns"),
    ("order_cpu_pressure", "order-service", "cpu-pressure"),
    ("order_memory_pressure", "order-service", "memory-pressure"),
    ("order_memory_oom", "order-service", "memory-oom"),
    ("inventory_process_stopped", "inventory-service", "stop"),
    ("inventory_table_read_blocked", "inventory-service", "inventory-table-lock"),
)


def inventory_read_lock() -> psycopg.Connection:
    """A real database lock, restricted to the isolated Lab inventory table."""
    connection = psycopg.connect(BUSINESS_DB_URL, connect_timeout=2)
    try:
        connection.execute("SET LOCAL lock_timeout = '2s'")
        connection.execute("SET LOCAL idle_in_transaction_session_timeout = '65s'")
        connection.execute("LOCK TABLE lab.inventory_items IN ACCESS EXCLUSIVE MODE")
        return connection
    except Exception:
        connection.close()
        raise


def docker_command(*args: str) -> str:
    return subprocess.check_output(["docker", *args], cwd=ROOT, text=True, timeout=30).strip()


def cpu_pressure_start() -> tuple[str, int, str]:
    container_id = docker_command("compose", "ps", "-q", "order-service")
    if not container_id:
        raise RuntimeError("order-service is not running")
    original_nano_cpus = int(docker_command(
        "inspect", "--format", "{{.HostConfig.NanoCpus}}", container_id
    ))
    marker = f"/tmp/aegisops-cpu-burn-{uuid4().hex}.pid"
    burn = (
        "import os,time\n"
        f"open({marker!r},'w').write(str(os.getpid()))\n"
        "end=time.monotonic()+90\n"
        "while time.monotonic()<end: pass\n"
    )
    docker_command("update", "--cpus", "0.05", container_id)
    try:
        docker_command("exec", "-d", container_id, "python", "-c", burn)
    except Exception:
        if original_nano_cpus == 0:
            docker_command("compose", "up", "-d", "--no-deps", "--force-recreate", "order-service")
        else:
            docker_command(
                "update", "--cpus", f"{original_nano_cpus / 1_000_000_000:g}", container_id
            )
        raise
    return container_id, original_nano_cpus, marker


def cpu_pressure_stop(state: tuple[str, int, str]) -> None:
    container_id, original_nano_cpus, marker = state
    stop = (
        "import os,pathlib,signal\n"
        f"p=pathlib.Path({marker!r})\n"
        "if p.exists():\n"
        "    pid=int(p.read_text())\n"
        "    try: os.kill(pid,signal.SIGTERM)\n"
        "    except ProcessLookupError: pass\n"
        "    p.unlink()\n"
    )
    try:
        docker_command("exec", container_id, "python", "-c", stop)
    finally:
        if original_nano_cpus == 0:
            docker_command("compose", "up", "-d", "--no-deps", "--force-recreate", "order-service")
        else:
            docker_command(
                "update", "--cpus", f"{original_nano_cpus / 1_000_000_000:g}", container_id
            )
        current_id = docker_command("compose", "ps", "-q", "order-service")
        current_nano_cpus = int(docker_command(
            "inspect", "--format", "{{.HostConfig.NanoCpus}}", current_id
        ))
        if current_nano_cpus != original_nano_cpus:
            raise RuntimeError("order-service CPU quota was not restored")


def memory_pressure_start(megabytes: int) -> tuple[str, int, int]:
    container_id = docker_command("compose", "ps", "-q", "order-service")
    if not container_id:
        raise RuntimeError("order-service is not running")
    limits = docker_command(
        "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}", container_id
    )
    original_memory, original_swap = (int(value) for value in limits.split())
    if original_memory != 0 or original_swap != 0:
        raise RuntimeError("memory pressure requires an unrestricted baseline container")
    burn = (
        "import time\n"
        f"data=bytearray({megabytes}*1024*1024)\n"
        "for index in range(0,len(data),4096): data[index]=1\n"
        "time.sleep(65)\n"
    )
    docker_command("update", "--memory", "128m", "--memory-swap", "128m", container_id)
    try:
        docker_command("exec", "-d", container_id, "python", "-c", burn)
    except Exception:
        docker_command("compose", "up", "-d", "--no-deps", "--force-recreate", "order-service")
        raise
    return container_id, original_memory, original_swap


def memory_pressure_stop(state: tuple[str, int, int]) -> None:
    docker_command("compose", "up", "-d", "--no-deps", "--force-recreate", "order-service")
    container_id = docker_command("compose", "ps", "-q", "order-service")
    limits = docker_command(
        "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}", container_id
    )
    restored = tuple(int(value) for value in limits.split())
    if restored != state[1:]:
        raise RuntimeError("order-service memory limit was not restored")


def set_provider_delay(client: httpx.Client, seconds: float) -> None:
    response = client.post(
        "http://127.0.0.1:8002/lab/faults/provider-delay",
        params={"seconds": seconds, "ttl_seconds": 90},
        headers={"X-Lab-Fault-Token": "aegisops_lab_fault_dev_only"},
    )
    response.raise_for_status()


def set_payment_dns_fault(client: httpx.Client, enabled: bool) -> None:
    response = client.post(
        "http://127.0.0.1:8003/lab/faults/payment-dns",
        params={"enabled": str(enabled).lower(), "ttl_seconds": 90},
        headers={"X-Lab-Fault-Token": "aegisops_lab_fault_dev_only"},
    )
    response.raise_for_status()


def checkout_details(
    client: httpx.Client, *, sku: str = "sku-demo", key: UUID | None = None
) -> tuple[int | None, dict | None]:
    request_key = key or uuid4()
    try:
        response = client.post(
            "http://127.0.0.1:8001/checkout",
            json={"idempotency_key": str(request_key), "sku": sku, "quantity": 1},
        )
        return response.status_code, response.json() if response.status_code == 200 else None
    except httpx.RequestError:
        return None, None


def checkout(client: httpx.Client, *, sku: str = "sku-demo") -> int | None:
    return checkout_details(client, sku=sku)[0]


def inventory_currency() -> str:
    with psycopg.connect(BUSINESS_DB_URL) as connection:
        row = connection.execute(
            "SELECT currency FROM lab.inventory_items WHERE sku = %s", ("sku-demo",)
        ).fetchone()
    if row is None:
        raise RuntimeError("sku-demo is missing")
    return str(row[0])


def set_inventory_currency(currency: str) -> None:
    with psycopg.connect(BUSINESS_DB_URL) as connection:
        connection.execute(
            "UPDATE lab.inventory_items SET currency = %s WHERE sku = %s",
            (currency, "sku-demo"),
        )


def persisted_checkout_counts(keys: list[UUID]) -> tuple[int, int]:
    with psycopg.connect(BUSINESS_DB_URL) as connection:
        row = connection.execute(
            "SELECT count(*), count(payment_id) FROM lab.orders "
            "WHERE idempotency_key = ANY(%s)",
            (keys,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def active_alerts(client: httpx.Client) -> list[str]:
    response = client.get("http://127.0.0.1:9093/api/v2/alerts")
    response.raise_for_status()
    return sorted({item["labels"].get("alertname", "") for item in response.json()})


def observed_since(started_at: datetime) -> list[dict]:
    with psycopg.connect(DB_URL) as connection:
        rows = connection.execute(
            """
            SELECT a.payload->>'alertname', a.service_name, a.received_at,
                   ia.incident_id::text, r.status, r.primary_hypothesis,
                   p.status, a.id::text, a.occurred_at, a.source, p.action_types
            FROM alerts a
            LEFT JOIN incident_alerts ia ON ia.alert_id = a.id AND ia.tenant_id = a.tenant_id
            LEFT JOIN LATERAL (
                SELECT status, primary_hypothesis FROM investigation_reports
                WHERE incident_id = ia.incident_id AND tenant_id = a.tenant_id
                ORDER BY source_incident_version DESC, completed_at DESC LIMIT 1
            ) r ON true
            LEFT JOIN LATERAL (
                SELECT p.status, (SELECT jsonb_agg(ac.snapshot->>'action_type')
                    FROM actions ac WHERE ac.plan_id = p.id AND ac.tenant_id = p.tenant_id
                ) AS action_types FROM remediation_plans p
                WHERE p.incident_id = ia.incident_id AND p.tenant_id = a.tenant_id
                ORDER BY p.created_at DESC LIMIT 1
            ) p ON true
            WHERE a.received_at >= %s
              AND a.tenant_id = %s
              AND a.payload->>'alertname' NOT LIKE '%%Holdout'
            ORDER BY a.received_at, a.id
            """,
            (started_at, Settings().dev_tenant_id),
        ).fetchall()
    return [
        {
            "alert": name,
            "service": service,
            "received_at": received.isoformat(),
            "incident_id": incident_id,
            "investigation_status": status,
            "hypothesis": hypothesis,
            "plan_status": plan_status,
            "alert_id": alert_id,
            "occurred_at": occurred.isoformat(),
            "source": source,
            "action_types": action_types,
        }
        for (name, service, received, incident_id, status, hypothesis, plan_status,
             alert_id, occurred, source, action_types) in rows
    ]


def wait_for_recovery(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if checkout(client) == 200:
            return
        time.sleep(2)
    raise RuntimeError("checkout did not recover after fault cleanup")


def run_case(client: httpx.Client, name: str, target: str, mode: str) -> dict:
    baseline_started = time.monotonic()
    baseline_status = checkout(client)
    baseline_checkout_seconds = round(time.monotonic() - baseline_started, 3)
    if baseline_status != 200:
        raise RuntimeError(f"baseline checkout failed before {name}")
    quiet_deadline = time.monotonic() + 85
    while active_alerts(client) and time.monotonic() < quiet_deadline:
        time.sleep(5)
    existing = active_alerts(client)
    if existing:
        raise RuntimeError(f"active alerts contaminate {name}: {existing}")
    started_at = datetime.now(UTC)
    original_currency: str | None = None
    cpu_state: tuple[str, int, str] | None = None
    memory_state: tuple[str, int, int] | None = None
    locked_connection: psycopg.Connection | None = None
    if mode == "provider-delay":
        set_provider_delay(client, 8)
    elif mode == "payment-dns":
        set_payment_dns_fault(client, True)
    elif mode == "inventory-currency-drift":
        original_currency = inventory_currency()
        set_inventory_currency("USD" if original_currency != "USD" else "EUR")
    elif mode == "cpu-pressure":
        cpu_state = cpu_pressure_start()
    elif mode in {"memory-pressure", "memory-oom"}:
        memory_state = memory_pressure_start(96 if mode == "memory-oom" else 64)
    elif mode == "inventory-table-lock":
        locked_connection = inventory_read_lock()
    else:
        compose(mode, target)
    try:
        effective_at = datetime.now(UTC)
        statuses: list[int | None] = []
        checkout_latency_seconds: list[float] = []
        checkout_bodies: list[dict | None] = []
        checkout_keys: list[UUID] = []
        cpu_sample: str | None = None
        memory_sample: str | None = None
        memory_events: str | None = None
        container_oom_killed: bool | None = None
        deadline = time.monotonic() + 42
        while time.monotonic() < deadline:
            key = uuid4()
            checkout_keys.append(key)
            checkout_started = time.monotonic()
            status, body = checkout_details(client, key=key)
            checkout_latency_seconds.append(round(time.monotonic() - checkout_started, 3))
            statuses.append(status)
            checkout_bodies.append(body)
            if cpu_state and cpu_sample is None:
                cpu_sample = docker_command(
                    "stats", "--no-stream", "--format", "{{.CPUPerc}}", cpu_state[0]
                )
            if memory_state and memory_sample is None:
                try:
                    memory_sample = docker_command(
                        "stats", "--no-stream", "--format", "{{.MemUsage}}", memory_state[0]
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    memory_sample = "container unavailable"
            if observed_since(started_at):
                # Keep the fault long enough for the investigation worker to read telemetry.
                time.sleep(5)
                break
            if mode == "inventory-currency-drift" and statuses:
                time.sleep(25)
                break
            time.sleep(4)
        control_status = client.get("http://127.0.0.1:8000/api/v1/health/ready").status_code
        if memory_state:
            container_oom_killed = docker_command(
                "inspect", "--format", "{{.State.OOMKilled}}", memory_state[0]
            ) == "true"
            try:
                memory_events = docker_command(
                    "exec", memory_state[0], "cat", "/sys/fs/cgroup/memory.events"
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                memory_events = "container unavailable"
    finally:
        if mode == "provider-delay":
            set_provider_delay(client, 0)
        elif mode == "payment-dns":
            set_payment_dns_fault(client, False)
        elif mode == "inventory-currency-drift":
            assert original_currency is not None
            set_inventory_currency(original_currency)
        elif mode == "cpu-pressure":
            assert cpu_state is not None
            cpu_pressure_stop(cpu_state)
        elif mode in {"memory-pressure", "memory-oom"}:
            assert memory_state is not None
            memory_pressure_stop(memory_state)
        elif mode == "inventory-table-lock":
            assert locked_connection is not None
            locked_connection.rollback()
            locked_connection.close()
        else:
            compose("unpause" if mode == "pause" else "start", target)
    wait_for_recovery(client)
    persisted_orders, persisted_payments = (
        persisted_checkout_counts(checkout_keys)
        if mode == "inventory-currency-drift" else (None, None)
    )
    time.sleep(12)  # Include alerts delivered just after the service has recovered.
    alerts = observed_since(started_at)
    report_deadline = time.monotonic() + 25
    while (
        any(item["incident_id"] and item["investigation_status"] is None for item in alerts)
        and time.monotonic() < report_deadline
    ):
        time.sleep(2)
        alerts = observed_since(started_at)
    first = datetime.fromisoformat(alerts[0]["received_at"]) if alerts else None
    incident_groups: dict[str, list[str]] = {}
    for item in alerts:
        if item["incident_id"]:
            incident_groups.setdefault(item["incident_id"], []).append(item["alert"])
    allowed_domains = {"inventory_unavailable"} if target == "inventory-service" and mode in {
        "pause", "stop", "inventory-table-lock"
    } else set()
    wrong_roots = list({
        item["incident_id"]: item for item in alerts
        if item["incident_id"] and item["investigation_status"] == "SUPPORTED"
        and item["hypothesis"] not in allowed_domains
    }.values())
    wrong_action_plans = [item for item in wrong_roots if item["plan_status"] is not None]
    with (ROOT / "src/aegisops/application/remediation/catalog.toml").open("rb") as catalog_file:
        catalog = tomllib.load(catalog_file)
    mapped_roots = {item["hypothesis_key"] for item in catalog["actions"]}
    eligible_wrong_actions = [
        item for item in wrong_roots
        if item["hypothesis"] in mapped_roots
    ]
    return {
        "fault": name,
        "expected_fault_domains": sorted(allowed_domains),
        "mode": mode,
        "target": target,
        "started_at": started_at.isoformat(),
        "activation_completed_at": effective_at.isoformat(),
        "evaluation_completed_at": datetime.now(UTC).isoformat(),
        "preexisting_alerts": existing,
        "checkout_statuses": statuses,
        "baseline_checkout_seconds": baseline_checkout_seconds,
        "checkout_latency_seconds": checkout_latency_seconds,
        "checkout_bodies": checkout_bodies if mode == "inventory-currency-drift" else None,
        "expected_currency": original_currency,
        "data_integrity_violation": (
            any(body and body.get("currency") != original_currency for body in checkout_bodies)
            if mode == "inventory-currency-drift" else False
        ),
        "persisted_fault_orders": persisted_orders,
        "persisted_fault_payments": persisted_payments,
        "control_api_status": control_status,
        "cpu_usage_sample": cpu_sample,
        "memory_usage_sample": memory_sample,
        "memory_events": memory_events,
        "container_oom_killed": container_oom_killed,
        "detected": bool(alerts),
        "detection_seconds_after_fault_activation": (
            round((first - started_at).total_seconds(), 2) if first else None
        ),
        "detection_clock": "fault_activation_request_to_alert_received",
        "alerts": alerts,
        "incident_partition": sorted(sorted(names) for names in incident_groups.values()),
        "incident_split_count": max(0, len(incident_groups) - 1),
        "wrong_supported_root_count": len(wrong_roots),
        "catalog_mapped_wrong_root_count": len(eligible_wrong_actions),
        "wrong_action_plan_count": len(wrong_action_plans),
        "recovered": True,
    }


def run_normal_checkout_control(client: httpx.Client) -> dict:
    quiet_deadline = time.monotonic() + 85
    while active_alerts(client) and time.monotonic() < quiet_deadline:
        time.sleep(5)
    if existing := active_alerts(client):
        raise RuntimeError(f"active alerts contaminate normal checkout control: {existing}")
    started_at = datetime.now(UTC)
    statuses: list[int | None] = []
    latencies: list[float] = []
    for _ in range(10):
        began = time.monotonic()
        statuses.append(checkout(client))
        latencies.append(round(time.monotonic() - began, 3))
        time.sleep(2)
    time.sleep(15)
    return {
        "fault": "normal_checkout_control",
        "started_at": started_at.isoformat(),
        "checkout_statuses": statuses,
        "checkout_latency_seconds": latencies,
        "alerts": observed_since(started_at),
    }


def main() -> None:
    require_dev_runtime(Settings())
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", choices=[case[0] for case in CASES])
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--skip-negative-control", action="store_true")
    parser.add_argument("--negative-control-only", action="store_true")
    parser.add_argument("--normal-checkout-only", action="store_true")
    args = parser.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "eval" / f"fault-battery-{run_id}.json"
    results = []
    with httpx.Client(timeout=12) as client:
        if args.normal_checkout_only:
            for repeat_index in range(1, args.repeat + 1):
                result = run_normal_checkout_control(client)
                result["repeat_index"] = repeat_index
                results.append(result)
                output.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps({
                    "fault": result["fault"],
                    "repeat_index": repeat_index,
                    "successes": result["checkout_statuses"].count(200),
                    "max_checkout_seconds": max(result["checkout_latency_seconds"]),
                    "alert_count": len(result["alerts"]),
                }, ensure_ascii=False), flush=True)
            print(f"report={output}")
            return
        for name, target, mode in CASES:
            if args.negative_control_only:
                break
            if args.cases and name not in args.cases:
                continue
            for repeat_index in range(1, args.repeat + 1):
                try:
                    result = run_case(client, name, target, mode)
                except Exception as error:
                    results.append({"fault": name, "repeat_index": repeat_index,
                                    "evaluation_failed": True,
                                    "error_code": type(error).__name__})
                    output.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                    raise
                result["repeat_index"] = repeat_index
                results.append(result)
                output.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps({
                    "fault": name,
                    "repeat_index": repeat_index,
                    "detected": result["detected"],
                    "latency_seconds": result["detection_seconds_after_fault_activation"],
                    "alerts": [item["alert"] for item in result["alerts"]],
                    "wrong_roots": result["wrong_supported_root_count"],
                    "wrong_action_plans": result["wrong_action_plan_count"],
                }, ensure_ascii=False), flush=True)
        if not args.skip_negative_control:
            quiet_deadline = time.monotonic() + 85
            while active_alerts(client) and time.monotonic() < quiet_deadline:
                time.sleep(5)
            if existing := active_alerts(client):
                raise RuntimeError(f"active alerts contaminate negative control: {existing}")
            negative_started = datetime.now(UTC)
            negative_status = checkout(client, sku="sku-does-not-exist")
            time.sleep(15)
            negative_alerts = observed_since(negative_started)
            negative = {
                "fault": "unknown_sku_negative_control",
                "checkout_status": negative_status,
                "detected": bool(negative_alerts),
                "alerts": negative_alerts,
            }
            results.append(negative)
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(negative, ensure_ascii=False), flush=True)
    print(f"report={output}")


if __name__ == "__main__":
    main()
