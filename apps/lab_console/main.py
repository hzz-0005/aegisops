"""Loopback-only console for testing the real Docker Lab and investigation flow."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aegisops.application.incident_request_evidence import AttachRequestEvidenceInput
from aegisops.application.request_signal_alerts import (
    AlertResolveInput,
    SignalPromotionInput,
)
from aegisops.config import Settings, require_dev_runtime

# One shared loopback client. httpx.Client() init loads the Windows certificate
# store and costs seconds, so a per-request client made every view multi-second.
CONTROL_CLIENT = httpx.Client(timeout=45, trust_env=False)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent / "static"
API_URL = os.getenv("AEGISOPS_API_URL", "http://127.0.0.1:8000")
GATEWAY_URL = "http://127.0.0.1:8001"
INVENTORY_URL = "http://127.0.0.1:8004"
SCENARIOS = {
    "redis": {
        "title": "Redis 不可用",
        "service": "Payment / Redis",
        "module": "lab.scenarios.redis_down",
        "description": "停止本项目 Redis，观察付款失败、告警和恢复。",
    },
    "db_pool": {
        "title": "订单数据库连接池耗尽",
        "service": "Order / PostgreSQL",
        "module": "lab.scenarios.db_pool_exhaustion",
        "description": "占满 Order 的连接池，观察结账失败和控制面隔离。",
    },
    "bad_deployment": {
        "title": "错误部署",
        "service": "Order / Deployment",
        "module": "lab.scenarios.bad_deployment",
        "description": "部署有故障的 Order 配置，观察变更关联与回滚。",
    },
}

settings = Settings()
require_dev_runtime(settings)
app = FastAPI(title="AegisOps Lab Console", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
runs: dict[str, dict] = {}
runs_lock = Lock()


class CheckoutInput(BaseModel):
    idempotency_key: UUID
    sku: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: int = Field(gt=0, le=100)


class LabAlertInput(BaseModel):
    """One synthetic alert; labels are facts, never a request to execute a tool."""

    service: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    alert_name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    resource: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    signal_type: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    severity: str = Field(default="warning", pattern=r"^(warning|critical)$")
    summary: str = Field(default="", max_length=300)


class LabApprovalInput(BaseModel):
    expected_version: int = Field(ge=1)
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    reason: str = Field(min_length=3, max_length=300)


def now() -> str:
    return datetime.now(UTC).isoformat()


def require_local(request: Request) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="local console only")


def require_mutation(request: Request, x_aegisops_console: str | None) -> None:
    require_local(request)
    origin = request.headers.get("origin")
    if origin and origin != os.getenv("AEGISOPS_CONSOLE_URL", "http://127.0.0.1:8010"):
        raise HTTPException(status_code=403, detail="invalid origin")
    if x_aegisops_console != "lab-console-v1":
        raise HTTPException(status_code=403, detail="console request header required")


def control_api(
    path: str, *, operator: bool = False, approver: bool = False,
    method: str = "GET", payload: dict | None = None,
) -> dict | list:
    token = (
        settings.approver_token
        if approver
        else settings.operator_token if operator else settings.alert_webhook_token
    )
    if not token:
        raise HTTPException(status_code=503, detail="development API token is unavailable")
    try:
        response = CONTROL_CLIENT.request(
            method,
            f"{API_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="AegisOps API is unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="not ready")
    if response.status_code >= 400:
        detail = response.json().get("detail", "AegisOps API rejected request")
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


def optional_api(path: str) -> dict | list | None:
    try:
        return control_api(path)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


def finish_run(run_id: str, module: str) -> None:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        output, _ = process.communicate()
        with runs_lock:
            runs[run_id].update(
                status="SUCCEEDED" if process.returncode == 0 else "FAILED",
                finished_at=now(),
                exit_code=process.returncode,
                output=output[-12000:],
            )
    except Exception as exc:  # Keep the console usable if the Lab command itself fails.
        with runs_lock:
            runs[run_id].update(
                status="FAILED", finished_at=now(), output=f"{type(exc).__name__}: {exc}"
            )


@app.get("/")
def index(request: Request) -> FileResponse:
    require_local(request)
    return FileResponse(ASSETS / "index.html")


@app.get("/api/overview")
def overview(request: Request) -> dict:
    require_local(request)
    try:
        control_api("/api/v1/health/ready")
        api_ready = True
    except HTTPException:
        api_ready = False
    incidents = control_api("/api/v1/incidents") if api_ready else []
    try:
        detector_runs = (
            control_api("/api/v1/request-failure-detector/runs?limit=12") if api_ready else []
        )
    except HTTPException:
        detector_runs = None
    try:
        late_runs = control_api(
            "/api/v1/request-failure-detector/runs?limit=12&compensation=true"
        ) if api_ready else []
    except HTTPException:
        late_runs = None
    with runs_lock:
        recent_runs = list(reversed(list(runs.values())[-12:]))
    return {
        "api_ready": api_ready,
        "model": settings.llm_model if settings.llm_api_key else None,
        "scenarios": [{"id": key, **value} for key, value in SCENARIOS.items()],
        "incidents": incidents,
        "detector_runs": detector_runs,
        "late_detector_runs": late_runs,
        "request_signals": control_api("/api/v1/request-failure-signals") if api_ready else [],
        "runs": recent_runs,
    }


@app.get("/api/catalog")
def catalog(request: Request) -> list:
    require_local(request)
    try:
        response = CONTROL_CLIENT.get(f"{INVENTORY_URL}/catalog", timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="inventory service is unavailable") from exc
    return response.json()


@app.post("/api/request-signals/{signal_id}/promote")
def promote_request_source(signal_id: UUID, body: SignalPromotionInput, request: Request,
                           x_aegisops_console: str | None = Header(default=None)) -> dict | list:
    require_mutation(request, x_aegisops_console)
    return control_api(f"/api/v1/request-failure-signals/{signal_id}/promote", method="POST",
                       payload=body.model_dump(mode="json"), operator=True)


@app.post("/api/checkout")
def checkout(
    body: CheckoutInput,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict:
    require_mutation(request, x_aegisops_console)
    try:
        response = CONTROL_CLIENT.post(
            f"{GATEWAY_URL}/checkout", json=body.model_dump(mode="json"), timeout=10
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail="checkout result uncertain; retry the same key"
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json().get("detail", "checkout failed"),
        )
    return response.json()


@app.post("/api/lab-alerts", status_code=202)
def inject_lab_alert(
    body: LabAlertInput,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict:
    require_mutation(request, x_aegisops_console)
    if not settings.alert_webhook_token:
        raise HTTPException(status_code=503, detail="development alert token is unavailable")
    fingerprint = uuid4().hex
    labels = {
        "alertname": body.alert_name,
        "service": body.service,
        "severity": body.severity,
    }
    if body.resource:
        labels["resource"] = body.resource
    if body.signal_type:
        labels["signal_type"] = body.signal_type
    try:
        response = CONTROL_CLIENT.post(
            f"{API_URL}/api/v1/alerts/ingest",
            headers={"Authorization": f"Bearer {settings.alert_webhook_token}"},
            json={
                "version": "4",
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "fingerprint": fingerprint,
                        "startsAt": now(),
                        "labels": labels,
                        "annotations": {"summary": body.summary},
                    }
                ],
            },
            timeout=10,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="alert ingestion is unavailable") from exc
    if response.status_code != 202 or response.json().get("accepted") != 1:
        raise HTTPException(status_code=502, detail="alert ingestion rejected the Lab alert")
    return {"accepted": 1, "fingerprint": fingerprint}


@app.post("/api/scenarios/{scenario_id}/run", status_code=202)
def start_scenario(
    scenario_id: str,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict:
    require_mutation(request, x_aegisops_console)
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="unknown Lab scenario")
    with runs_lock:
        if any(run["status"] == "RUNNING" for run in runs.values()):
            raise HTTPException(status_code=409, detail="another Lab scenario is running")
        run_id = str(uuid4())
        runs[run_id] = {
            "id": run_id,
            "scenario_id": scenario_id,
            "title": scenario["title"],
            "status": "RUNNING",
            "started_at": now(),
            "finished_at": None,
            "exit_code": None,
            "output": "",
        }
    Thread(target=finish_run, args=(run_id, scenario["module"]), daemon=True).start()
    return runs[run_id]


@app.get("/api/runs/{run_id}")
def get_run(run_id: UUID, request: Request) -> dict:
    require_local(request)
    with runs_lock:
        run = runs.get(str(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return dict(run)


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: UUID, request: Request) -> dict:
    require_local(request)
    path = f"/api/v1/incidents/{incident_id}"
    paths = {
        "incident": (control_api, path),
        "timeline": (control_api, f"{path}/timeline"),
        "workflow": (optional_api, f"{path}/workflow"),
        "investigation": (optional_api, f"{path}/investigation"),
        "trace_overlaps": (control_api, f"{path}/trace-overlaps"),
        "request_candidates": (optional_api, f"{path}/request-candidates"),
        "request_evidence": (control_api, f"{path}/request-evidence"),
        "advice": (optional_api, f"{path}/investigation/advice"),
        "model_review": (optional_api, f"{path}/investigation/model-review"),
        "remediation_plan": (optional_api, f"{path}/remediation-plan"),
        "recovery": (
            lambda url: control_api(url, operator=True),
            f"{path}/recovery-assessment",
        ),
    }
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        futures = {key: pool.submit(fn, url) for key, (fn, url) in paths.items()}
        result = {key: future.result() for key, future in futures.items()}
    timeline = result["timeline"]
    if not isinstance(timeline, list):
        raise HTTPException(status_code=502, detail="invalid incident timeline response")
    approval_id = next(
        (
            event.get("details", {}).get("approval_id")
            for event in reversed(timeline)
            if event.get("event_type") == "POLICY_DECIDED"
            and event.get("details", {}).get("approval_id")
        ),
        None,
    )
    result["approval"] = (
        control_api(f"/api/v1/approvals/{approval_id}", approver=True)
        if approval_id else None
    )
    return result


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_promoted_alert(
    alert_id: UUID,
    body: AlertResolveInput,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict:
    require_mutation(request, x_aegisops_console)
    return control_api(
        f"/api/v1/alerts/{alert_id}/resolve",
        operator=True, method="POST", payload=body.model_dump(),
    )


@app.post("/api/incidents/{incident_id}/request-evidence", status_code=201)
def attach_incident_request_evidence(
    incident_id: UUID,
    body: AttachRequestEvidenceInput,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict | list:
    require_mutation(request, x_aegisops_console)
    return control_api(
        f"/api/v1/incidents/{incident_id}/request-evidence",
        operator=True,
        method="POST",
        payload=body.model_dump(mode="json"),
    )


@app.post("/api/incidents/{incident_id}/remediation-plan")
def propose_remediation(
    incident_id: UUID,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict | list:
    require_mutation(request, x_aegisops_console)
    return control_api(
        f"/api/v1/incidents/{incident_id}/remediation-plan", operator=True, method="POST"
    )


@app.post("/api/incidents/{incident_id}/remediation-plan/authorize")
def authorize_remediation(
    incident_id: UUID,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict | list:
    require_mutation(request, x_aegisops_console)
    return control_api(
        f"/api/v1/incidents/{incident_id}/remediation-plan/authorize",
        operator=True,
        method="POST",
    )


@app.post("/api/approvals/{approval_id}/decision")
def decide_lab_approval(
    approval_id: UUID,
    body: LabApprovalInput,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict:
    require_mutation(request, x_aegisops_console)
    if not settings.approver_token:
        raise HTTPException(status_code=503, detail="development approver token is unavailable")
    try:
        response = CONTROL_CLIENT.post(
            f"{API_URL}/api/v1/approvals/{approval_id}/decision",
            headers={"Authorization": f"Bearer {settings.approver_token}"},
            json=body.model_dump(mode="json"),
            timeout=45,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="approval API is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json().get("detail", "approval rejected"),
        )
    return response.json()


@app.post("/api/incidents/{incident_id}/model-review")
def model_review(
    incident_id: UUID,
    request: Request,
    x_aegisops_console: str | None = Header(default=None),
) -> dict | list:
    require_mutation(request, x_aegisops_console)
    return control_api(
        f"/api/v1/incidents/{incident_id}/investigation/model-review",
        operator=True,
        method="POST",
    )
