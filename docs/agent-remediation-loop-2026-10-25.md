# 审批修复闭环：自主调查到受控执行的首条端到端链路

2026-10-25，北京时间。P3 的核心验收：一条真实 Redis 故障走完 自主调查→门禁→映射→方案→OPA→独立审批→执行→恢复核验→结案。证据：`eval/agent-remediation-20261025T053238Z.json`（前10步）+ `eval/agent-remediation-completion-20261025T054157Z.json`（后3步，因脚本两处缺陷分两段保存，均为真实数据）。

## 链路与证据

| 步骤 | 结果 |
| --- | --- |
| 拒绝路径 | 未过门禁的 Agent 报告（盲测事故 2700345c）提议 409 "a gate-supported candidate with a mapped governed action is required"（部署后实测，两次运行均验证） |
| 故障与信号 | `compose stop redis`，窗口起点 ±30 秒内恰好 2 次结账失败（502），payment 上报 2 条 `dependency_transport`/redis-main 请求事件；10 分钟窗口成熟（+2 分钟水位）后检测器产出信号：2 成员/2 trace/VERIFIED_SPAN_FACT |
| 提升 | 操作员经 `POST /request-failure-signals/{id}/promote` 提升为 `VerifiedRequestFailureWindow` 告警（直接来源绑定），独立成事故 |
| 自主调查 | 真实模型（readonly-planner-v5-zh）：3 模型/2 工具，claim `REQUEST_FAILURE payment-service→redis-main dependency_transport` 被 `OBSERVATION_VERIFIED`，门禁 **MECHANISM_SCOPE_SUPPORTED + FULL_DIRECT_SOURCE（2/2）** |
| 映射与提议 | 确定性映射命中 `restart_cache/redis-main`（201）；候选文本不进入动作参数 |
| 治理 | OPA remediation-policy-v4 → REQUIRE_APPROVAL；独立审批人 APPROVE（版本/过期/kill switch/环境检查全过） |
| 执行 | lab-executor 审批后 9 秒接单：前置条件复读（redis exited + PING 不可用）→ Docker 重启 → PING/就绪恢复 → 唯一 attempt SUCCEEDED |
| 收尾 | 操作员核实解除症状告警（新端点，审计时间线）→ 恢复评估 `recovery_ready=true` 零限制项 → Temporal RESOLVE → 事故 RESOLVED |

## 本批改动

- `lab/services/payment.py`：`pay()` 的 redis 缓存读失败经 `mark_request_failure` 上报业务请求失败事实（resource=redis-main，timeout/transport 分码）；健康探针不上报，避免探针挤占调用方证据。
- `compose.yaml`/`.env.example`：payment 事件上报环境变量 + API `AEGISOPS_REQUEST_EVENT_SERVICE_TOKENS` 增加 payment-service。
- `remediation/agent_mapping.py`：映射键改为 `redis-main`（与资源 slug 一致）。
- `request_signal_alerts.py` 新增 `resolve_signal_alert` + API `POST /api/v1/alerts/{id}/resolve`（操作员令牌+理由；仅限 request-detector 来源的 OBSERVED 告警；幂等；写事故时间线）。提升告警没有 Alertmanager 生命周期，没有它恢复评估永远 false——这是请求信号路径的真实缺口。
- 提示 `readonly-planner-v5-zh`：来源覆盖核对是结论的一部分（结论前先 request_membership）+ claim 必须引用窗口内全部已知失败样本。v4 首跑因模型未调 request_membership 门禁 UNBOUND（失败证据 `eval/agent-remediation-20261025T051225Z.json` 保留）。
- `scripts/evaluate_agent_remediation.py`：端到端 harness（真值分离，含拒绝路径检查；首轮 poll 非终态即返回的缺陷已修）。

## 缺陷与教训（首轮运行暴露）

1. v4 模型取到精确事实并填了 claim，但没核对来源覆盖就 FINISH → 门禁 UNBOUND。v5 把覆盖核对写进结论义务后一次通过。提示词只增加认识论义务，未泄露任何真值。
2. 脚本 poll 把非终态 AUTHORIZED 当终态返回（执行器实际 9 秒接单）→ 已修为等待终态集合。
3. resolve 端点签名与调用不一致（tenant_id）→ 500 非 JSON → 已修并实测。

## 边界

- 映射表只有 Redis 一条（语义有据 + 执行前置条件兼容）；order 池耗尽路径的已核验事实只有网关 5xx（机制浅），不安全映射，保持拒绝。
- P4/生产适配不变；Agent 报告永不直接产出动作参数；映射表扩张需逐条评审。
- 本链路用了"操作员提升"作为直接来源入口——自动提升（不提升也能过门禁）不是本批目标。
