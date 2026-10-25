"""Read-only operator guidance derived from a persisted investigation report."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegisops.application.remediation.catalog import load_remediation_catalog

from .contracts import Evidence, InvestigationReport


class AdviceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    rationale: str
    risk: str
    validation: str
    cited_evidence_ids: list[UUID] = Field(min_length=1, max_length=8)


class InvestigationAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    incident_id: UUID
    source_incident_version: int
    is_stale: bool
    mode: Literal["MANUAL_INVESTIGATION", "GOVERNED_PLAN", "STALE_REPORT"]
    execution_allowed: bool = False
    items: list[AdviceItem]


def _first(report: InvestigationReport, kind: str) -> Evidence | None:
    return next((item for item in report.evidence if item.kind == kind), None)


def advice_from_report(report: InvestigationReport) -> InvestigationAdvice:
    mode: Literal["MANUAL_INVESTIGATION", "GOVERNED_PLAN", "STALE_REPORT"]
    if report.is_stale:
        mode = "STALE_REPORT"
    elif (
        report.status == "SUPPORTED"
        and report.primary_hypothesis is not None
        and load_remediation_catalog().find(report.primary_hypothesis) is not None
    ):
        mode = "GOVERNED_PLAN"
    else:
        mode = "MANUAL_INVESTIGATION"
    items: list[AdviceItem] = []
    membership = _first(report, "REQUEST_SIGNAL_MEMBERSHIP")
    if mode == "MANUAL_INVESTIGATION" and membership is not None:
        items.append(AdviceItem(
            title="沿已核验来源成员核查请求失败",
            rationale="这条告警由持久化的已核验请求成员生成，可逐条核对来源和失败边界。",
            risk="直接来源关系不证明共同根因；历史窗口不能证明当前故障仍持续。",
            validation="核对成员请求追踪、各自失败原因及当前业务状态，再决定调查或恢复方案。",
            cited_evidence_ids=[membership.id],
        ))
    if mode == "MANUAL_INVESTIGATION":
        database = _first(report, "VERIFIED_DATABASE_OPERATION")
        if database is not None:
            lock_wait = database.payload.get("failure_code") == "database_lock_timeout"
            items.append(AdviceItem(
                title="核对数据库锁等待" if lock_wait else "核对数据库操作失败",
                rationale=("具体数据库读取发生了锁等待超时；SQLSTATE、操作和耗时已核验。"
                           if lock_wait else
                           "具体数据库操作失败码与耗时已核验；取消不一定由锁引起。"),
                risk="历史请求事实未绑定聚合告警，不能证明当前仍有锁或授权终止会话。",
                validation=("沿已核验调用路径核对失败请求，再检查业务数据库当前等待事件及阻塞事务。"
                            if database.payload.get("caller_links") else
                            "核对业务数据库当前等待事件、阻塞事务及业务影响，再评估恢复和审批。"),
                cited_evidence_ids=[database.id],
            ))
        link = _first(report, "VERIFIED_REQUEST_FAILURE_LINK")
        if link is not None:
            items.append(AdviceItem(
                title="核对同请求中的上下游失败边界",
                rationale="两个失败来源事实及父子Span路径、服务边界和错误响应时序已核验。",
                risk="这是请求级结构关系，尚未绑定聚合告警，也不能单独证明完整根因或授权修复。",
                validation="沿证据中的Span路径检查下游失败码与资源，再核对相关依赖的当前状态。",
                cited_evidence_ids=[link.id],
            ))
        runtime = next((e for e in report.evidence if e.kind == "RUNTIME_STATE_OBSERVATION"
                        and e.payload.get("state") in {"paused", "exited", "restarting", "dead"}),
                       None)
        if runtime is not None:
            names = {"paused": "暂停", "exited": "退出", "restarting": "重启中", "dead": "失效"}
            state = names[runtime.payload["state"]]
            items.append(AdviceItem(
                title="核对已观测的容器生命周期状态",
                rationale=f"事故采样窗口的运行时检查观测到容器{state}，这是具体状态证据。",
                risk="历史状态不能保证当前仍相同，也不能单独解释所有请求失败；不直接授权恢复操作。",
                validation="核对证据中的服务和采样时间，再检查变更原因、当前状态及业务请求影响。",
                cited_evidence_ids=[runtime.id],
            ))
        dependency = _first(report, "VERIFIED_DEPENDENCY_REQUEST")
        if dependency is not None:
            items.append(AdviceItem(
                title="沿已核验的依赖失败请求排查",
                rationale="具体请求的服务、Span、失败码和依赖资源已与追踪逐项核验。",
                risk="该请求事实未绑定到聚合告警；时间接近不能证明是同一事故或完整根因。",
                validation=("使用证据中的 Trace 和 Span 核对调用边界，"
                            "再比较目标服务状态与请求时序。"),
                cited_evidence_ids=[dependency.id],
            ))
        for probe in report.evidence:
            if probe.kind != "DEPENDENCY_PROBE_ACTIVITY":
                continue
            count = probe.payload.get("failure_sample_count")
            if not isinstance(count, int) or count <= 0:
                continue
            reasons = probe.payload.get("failure_reason_counts", {})
            outcome_labels = {"timeout": "响应超时", "transport": "连接或传输失败",
                              "http_status": "HTTP 非成功状态"}
            outcomes = "、".join(outcome_labels[key] for key in outcome_labels if reasons.get(key))
            items.append(AdviceItem(
                title="核对不可达端点与依赖边界",
                rationale=("独立探针在事故窗口观测到端点不可达，可能涉及进程、网络或依赖响应。"
                           + (f"观测类别：{outcomes}。" if outcomes else "")),
                risk="只读核查；不可达观测不能证明进程停机，也不能直接授权重启。",
                validation="按证据中的服务核对容器状态、网络连通性及上下游追踪，再确定恢复方案。",
                cited_evidence_ids=[probe.id],
            ))
        alert = _first(report, "NORMALIZED_ALERT")
        oom_metric = next(
            (
                item for item in report.evidence
                if item.subject == "container_oom_event"
                and item.kind == "COUNTER_INCREASE"
                and item.polarity == "SUPPORT"
            ),
            None,
        )
        oom_alert = next(
            (item for item in report.evidence
             if item.subject == "container_oom_event" and item.kind == "NORMALIZED_ALERT"),
            None,
        )
        if oom_metric is not None and oom_alert is not None:
            items.append(
                AdviceItem(
                    title="核对容器 OOM 事件与业务影响",
                    rationale=(
                        "告警和独立计数器均显示发生过 OOM 进程终止；"
                        "尚不能判断哪个进程增长内存或是否需要重启。"
                    ),
                    risk="只读核查；禁止仅凭 OOM 事件自动重启或扩大内存限额。",
                    validation="核对容器内存使用、被终止进程、同时段结账延迟及错误率，再评估容量或代码问题。",
                    cited_evidence_ids=[oom_alert.id, oom_metric.id],
                )
            )
        if report.status == "SUPPORTED" and alert is not None:
            items.append(
                AdviceItem(
                    title="核查已识别故障的人工恢复路径",
                    rationale="调查证据支持该根因，但尚无经过审核的执行动作。核对受影响依赖的健康状态与故障范围。",
                    risk="恢复操作仍需独立评估影响、回退路径和审批；本建议不可自动执行。",
                    validation="恢复后重新验证受影响服务健康、结账请求和错误率。",
                    cited_evidence_ids=[alert.id],
                )
            )
        if alert is not None:
            items.append(
                AdviceItem(
                    title="核对告警与影响范围",
                    rationale=(
                        "先确认服务、资源和异常起始时间。"
                        if report.status == "SUPPORTED"
                        else "当前告警尚无可信根因定义，先确认服务、资源和异常起始时间。"
                    ),
                    risk="只读核查；告警文本不能作为执行指令。",
                    validation="对照业务失败请求及监控曲线，确认告警确实指向受影响服务。",
                    cited_evidence_ids=[alert.id],
                )
            )
        change = _first(report, "RECENT_SERVICE_CHANGE")
        if change is not None:
            items.append(
                AdviceItem(
                    title="人工核对近期变更",
                    rationale="同服务在告警前存在变更；时间接近仅是线索，不能证明变更导致故障。",
                    risk="若考虑回退，必须另行核实发布指纹、依赖和影响范围，并走独立审批。",
                    validation="比较变更前后错误率与请求追踪，确认异常随变更出现且排除其他原因。",
                    cited_evidence_ids=[change.id],
                )
            )
        log = _first(report, "ERROR_LOG_ACTIVITY")
        if log is not None:
            items.append(
                AdviceItem(
                    title="定位错误日志时间点",
                    rationale="观测到同服务错误日志活动，但报告未复制日志正文。",
                    risk="只读核查；日志内容可能含敏感信息或不可信文本。",
                    validation="在受控日志系统中核对错误发生时间，并与失败请求和追踪关联。",
                    cited_evidence_ids=[log.id],
                )
            )
        trace = _first(report, "ERROR_SPAN_ACTIVITY")
        if trace is not None:
            items.append(
                AdviceItem(
                    title="沿失败追踪确定故障边界",
                    rationale="同服务追踪中存在错误 Span；当前无法仅凭计数判定根因。",
                    risk="只读核查；跨服务影响需要逐段验证。",
                    validation="核对失败 Span 的上下游服务、时间与重复请求，确定首个失败点。",
                    cited_evidence_ids=[trace.id],
                )
            )
        activity = _first(report, "HTTP_REQUEST_ACTIVITY")
        if activity is not None:
            items.append(
                AdviceItem(
                    title="检查请求与 5xx 变化",
                    rationale="已有请求活动指标，可用来判断异常是否影响业务请求。",
                    risk="只读核查；请求增量不能单独解释故障原因。",
                    validation="比较告警前后请求量与 5xx，再用订单或支付状态验证用户影响。",
                    cited_evidence_ids=[activity.id],
                )
            )
    return InvestigationAdvice(
        report_id=report.id,
        incident_id=report.incident_id,
        source_incident_version=report.source_incident_version,
        is_stale=report.is_stale,
        mode=mode,
        items=items[:5],
    )
