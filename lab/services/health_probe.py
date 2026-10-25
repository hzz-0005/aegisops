"""Independent, generic availability probes for the business Lab services."""

import asyncio
import json
import os
import time
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from lab.services.business_contracts import ASSERTIONS

app = FastAPI(title="AegisOps Lab health probe")
TARGETS: dict[str, str] = json.loads(os.environ.get("LAB_PROBE_TARGETS", "{}"))


@dataclass(frozen=True)
class ProbeObservation:
    service: str
    success: int
    failure: str
    duration: float


async def probe(client: httpx.AsyncClient, service: str, url: str) -> ProbeObservation:
    started = time.monotonic()
    failure = "none"
    try:
        response = await client.get(url)
        success = int(200 <= response.status_code < 300)
        if not success:
            failure = "http_status"
    except httpx.TimeoutException:
        success, failure = 0, "timeout"
    except httpx.HTTPError:
        success, failure = 0, "transport"
    return ProbeObservation(service, success, failure, time.monotonic() - started)


async def probe_assertion(
    client: httpx.AsyncClient, spec: dict[str, str]
) -> tuple[str, str, int, int]:
    # No usable response means unobserved, never proof of incorrect business data.
    success, evaluated = 0, 0
    try:
        response = await client.get(spec["url"])
        if response.status_code == 200:
            actual = response.json()
            if isinstance(actual, dict):
                evaluated = 1
                success = int(actual.get(spec["field"]) == spec["equals"])
    except (httpx.HTTPError, ValueError):
        pass
    return spec["name"], spec["service"], success, evaluated


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
        results = await asyncio.gather(
            *(probe(client, service, url) for service, url in sorted(TARGETS.items()))
        )
        assertion_results = await asyncio.gather(
            *(probe_assertion(client, spec) for spec in ASSERTIONS)
        )
    lines = [
        "# HELP lab_probe_success Business Lab service health endpoint is reachable",
        "# TYPE lab_probe_success gauge",
    ]
    lines.extend(
        f'lab_probe_success{{service="{result.service}"}} {result.success}'
        for result in results
    )
    lines.extend([
        "# HELP lab_probe_duration_seconds Latest endpoint probe elapsed time",
        "# TYPE lab_probe_duration_seconds gauge",
        "# HELP lab_probe_failure Latest probe outcome by bounded failure category",
        "# TYPE lab_probe_failure gauge",
    ])
    for result in results:
        lines.append(
            f'lab_probe_duration_seconds{{service="{result.service}"}} {result.duration:.6f}'
        )
        for reason in ("timeout", "transport", "http_status"):
            lines.append(
                f'lab_probe_failure{{service="{result.service}",reason="{reason}"}} '
                f'{int(result.failure == reason)}'
            )
    lines.extend([
        "# HELP lab_probe_assertion_evaluated Business assertion has a usable JSON response",
        "# TYPE lab_probe_assertion_evaluated gauge",
    ])
    lines.extend(
        f'lab_probe_assertion_evaluated{{name="{name}",service="{service}"}} {evaluated}'
        for name, service, _, evaluated in assertion_results
    )
    lines.extend([
        "# HELP lab_probe_assertion_success Configured business response invariant holds",
        "# TYPE lab_probe_assertion_success gauge",
    ])
    lines.extend(
        f'lab_probe_assertion_success{{name="{name}",service="{service}"}} {success}'
        for name, service, success, _ in assertion_results
    )
    return "\n".join(lines) + "\n"
