"""Internal GET-only observer; select state fields from fixed business container names."""

import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

TARGETS = {service: f"aegisops-{service}-1" for service in (
    "lab-gateway", "inventory-service", "order-service", "payment-service",
)}
STATES = {"running", "paused", "exited", "restarting", "created", "dead", "removing"}


def observe(http: httpx.Client, service: str) -> dict:
    response = http.get(f"http://docker/containers/{TARGETS[service]}/json")
    response.raise_for_status()
    body = response.json()
    labels = body.get("Config", {}).get("Labels", {})
    if (labels.get("com.docker.compose.project") != "aegisops"
            or labels.get("com.docker.compose.service") != service):
        raise ValueError("container identity does not match the allowlist")
    state = body["State"]
    status = state["Status"]
    if status not in STATES or type(state.get("Paused")) is not bool:
        raise ValueError("invalid runtime state")
    if state["Paused"]:
        status = "paused"
    started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
    return {"state": status, "observed_at": time.time(),
            "started_at": max(0, started.timestamp())}


def render_metrics(socket_path: str = "/var/run/docker.sock") -> str:
    lines = ["# TYPE lab_runtime_observed gauge", "# TYPE lab_runtime_state gauge",
             "# TYPE lab_runtime_observed_at_seconds gauge",
             "# TYPE lab_runtime_started_at_seconds gauge"]
    with httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), timeout=1.5,
                      trust_env=False) as http:
        for service in TARGETS:
            try:
                result = observe(http, service)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                lines.append(f'lab_runtime_observed{{service="{service}"}} 0')
                continue
            lines.extend([
                f'lab_runtime_observed{{service="{service}"}} 1',
                f'lab_runtime_observed_at_seconds{{service="{service}"}} {result["observed_at"]}',
                f'lab_runtime_started_at_seconds{{service="{service}"}} {result["started_at"]}',
            ])
            for state in sorted(STATES):
                lines.append(f'lab_runtime_state{{service="{service}",state="{state}"}} '
                             f'{int(result["state"] == state)}')
    return "\n".join(lines) + "\n"


def start_observer() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                body = render_metrics().encode("utf-8")
            except Exception:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 8011), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server
