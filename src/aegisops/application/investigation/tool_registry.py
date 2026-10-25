"""Fixed read-only calls; planner cannot supply tenants, windows, queries or URLs."""

from collections.abc import Callable
from typing import Any

from .agent_contracts import ToolRequest
from .contracts import Evidence
from .read_tools import SERVICE_NAME, ReadOnlyTelemetryTools

TOOL_DESCRIPTIONS = {
    "http_activity": "计数类：请求/5xx峰值增量。只证明规模，不识别依赖或失败码，不能支撑claim",
    "error_logs": "计数类：错误日志条数（无正文）。只证明存在错误日志，不能支撑claim",
    "error_traces": "计数类：错误Span个数（无属性）。只证明存在失败Span，不能支撑claim",
    "probe_health": "计数类：端点成功/超时/传输采样次数。不证明机制，不能支撑claim",
    "runtime_state": "机制类：容器历史状态。精确观测可支撑RUNTIME_STATE claim；非当前状态",
    "incoming_requests": "机制类：登记上游逐请求失败Span含失败码，可支撑REQUEST_FAILURE claim",
    "dependency_requests": "机制类：下游依赖逐请求失败Span含失败码，可支撑REQUEST_FAILURE claim",
    "database_requests": "机制类：数据库操作/SQLSTATE/锁等待；不能支撑REQUEST_FAILURE claim",
    "request_links": "机制类：父Span路径验证的失败传播；连通不代表同根因，不能支撑claim",
    "request_membership": "来源类：告警直接来源成员与完整数量；聚合告警无绑定；不证明根因",
}


class ToolRegistry:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot

    def validate(self, request: ToolRequest) -> dict:
        alert = next((a for a in self.snapshot["alerts"] if a["id"] == request.alert_id), None)
        if alert is None:
            raise ValueError("alert outside incident snapshot")
        allowed = {alert["service"], *self.snapshot["dependencies"].get(alert["service"], [])}
        if request.service not in allowed or not SERVICE_NAME.fullmatch(request.service):
            raise ValueError("target outside incident dependency scope")
        if request.name == "request_membership" and request.service != alert["service"]:
            raise ValueError("membership must target selected alert service")
        return alert

    def fingerprint(self, request: ToolRequest) -> str:
        # Different alert IDs at the same instant must not reset a repeated-query budget.
        alert = self.validate(request)
        return f"{request.name}:{request.service}:{alert['occurred_at'].isoformat()}"

    def execute(self, tools: ReadOnlyTelemetryTools, request: ToolRequest) -> list[Evidence]:
        alert = self.validate(request)
        kwargs: dict[str, Any] = {"service": request.service, "occurred_at": alert["occurred_at"]}
        name = request.name
        result: Evidence | list[Evidence] | None
        if name == "request_membership":
            result = tools.request_alert_membership(
                tenant_id=self.snapshot["tenant_id"], alert_id=request.alert_id,
            )
        elif name in {"probe_health", "runtime_state"}:
            call = (tools.generic_probe_health if name == "probe_health"
                    else tools.generic_runtime_state)
            result = call(services=[request.service], occurred_at=alert["occurred_at"])
        elif name == "database_requests":
            result = tools.generic_database_requests(
                tenant_id=self.snapshot["tenant_id"], services=[request.service],
                occurred_at=alert["occurred_at"],
            )
        elif name == "dependency_requests":
            result = tools.generic_dependency_requests(
                tenant_id=self.snapshot["tenant_id"],
                dependencies=self.snapshot["dependencies"].get(request.service, []), **kwargs,
            )
        else:
            calls: dict[str, Callable[..., Evidence | list[Evidence] | None]] = {
                "http_activity": tools.generic_http_activity,
                "error_logs": tools.generic_error_logs,
                "error_traces": tools.generic_error_traces,
                "incoming_requests": tools.generic_incoming_requests,
                "request_links": tools.generic_request_links,
            }
            if name in {"incoming_requests", "request_links"}:
                kwargs["tenant_id"] = self.snapshot["tenant_id"]
            result = calls[name](**kwargs)
        items = [] if result is None else result if isinstance(result, list) else [result]
        # All adapters already have bounded query/window limits. Cap persisted observations too.
        scope = {"tenant_id": str(self.snapshot["tenant_id"]), "alert_id": str(request.alert_id),
                 "target_service": request.service, "occurred_at": alert["occurred_at"].isoformat(),
                 "tool": name}
        return [item.model_copy(update={"payload": {**item.payload, "investigation_scope": scope}})
                for item in items[:12]]
