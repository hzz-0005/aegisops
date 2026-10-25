import os

import httpx
from fastapi import HTTPException, Request
from opentelemetry import metrics, propagate

from lab.services.common import create_app, mark_request_failure
from lab.services.contracts import CreateOrder, OrderResult

app = create_app("lab-gateway")
upstream_failures = metrics.get_meter("aegisops.lab.gateway").create_counter(
    "lab_gateway_order_failures", description="Gateway checkouts failed by the order service"
)
upstream_failures.add(0, {"service": "lab-gateway"})


@app.post("/checkout", response_model=OrderResult)
def checkout(request: CreateOrder, http_request: Request) -> OrderResult:
    headers: dict[str, str] = {}
    propagate.inject(headers)
    try:
        response = httpx.post(
            f"{os.environ['LAB_ORDER_URL']}/orders",
            json=request.model_dump(mode="json"),
            headers=headers,
            timeout=5,
        )
    except httpx.HTTPError as exc:
        upstream_failures.add(1, {"service": "lab-gateway"})
        mark_request_failure(
            http_request, service="lab-gateway",
            failure_code="upstream_transport", resource="order-service",
        )
        raise HTTPException(status_code=502, detail="order service unavailable") from exc
    if response.status_code != 200:
        if response.status_code >= 500:
            attributes = {"service": "lab-gateway"}
            if change_id := response.headers.get("x-lab-change-id"):
                attributes["change_id"] = change_id
            upstream_failures.add(1, attributes)
            mark_request_failure(
                http_request, service="lab-gateway",
                failure_code="upstream_http_5xx", resource="order-service",
            )
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))
    return OrderResult.model_validate(response.json())
