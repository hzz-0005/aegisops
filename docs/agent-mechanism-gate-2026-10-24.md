# 自主调查第三批：机制取证引导与独立根因门禁

2026-10-24，北京时间。接续 `agent-observation-verification-2026-10-13.md` 的"继续工作"第1、2条。本批已部署、真实模型实测并完成视觉验收；根因定论与审批执行闭环仍未完成。

## 实现结果

- 工具描述全部标注证明类别：计数类（http_activity/error_logs/error_traces/probe_health）只证明规模或存在性、不能支撑claim；机制类（incoming_requests/dependency_requests/database_requests/runtime_state/request_links）给出依赖、失败码、状态或传播路径等精确事实；来源类（request_membership）核对直接来源覆盖。数据库工具明确标注不能支撑REQUEST_FAILURE claim。
- 系统提示升 `readonly-planner-v3-zh`：已有失败规模而 `coverage.exact_fact_count=0` 时优先机制工具，机制事实不能由计数工具推导；claim 只能照抄工具返回的确切身份，不得推测或组合。模型输入 coverage 新增 exact_fact_count。图版本 `readonly-agent-v3-mechanism-gate`，预算不变（4模型/8工具/90秒）。
- 新模块 `root_cause_gate.py`（root-cause-gate-v1）：纯函数消费 `candidate_verifications`，对每个候选输出 `RootCauseGateVerdict`。`MECHANISM_SCOPE_SUPPORTED` 要求 OBSERVATION_VERIFIED 且 DIRECT_SOURCE_MEMBERSHIP 且来源成员全部核验；否则 `GATE_NOT_PASSED` 并给出缺口（NO_VERIFIED_CLAIM / MECHANISM_UNPROVEN / CONFLICTED_OBSERVATIONS、SOURCE_COVERAGE_PARTIAL / SOURCE_SCOPE_UNBOUND），恒附加 CURRENT_FAULT_STATUS_UNKNOWN。root_cause_status 恒 UNDETERMINED、current_fault_status 恒 UNKNOWN、action_eligible 恒 False。结果存 agent_trace JSONB（无新迁移），报告与 API 暴露 `root_cause_gates`；陈旧报告降级为 GATE_NOT_PASSED+SNAPSHOT_CHANGED；旧报告缺字段默认空列表。
- 修正验证器引用规则：此前要求候选的全部支持引用来自已完成工具步骤，导致模型合法引用告警上下文证据时整条被拦（真实运行复现）。现改为：引用必须是持久化后端记录（已完成工具结果∪快照初始证据），且 claim 至少要有一条已完成、属性匹配的工具事实支撑；伪造引用仍然失败关闭。
- 工作台在候选核验行后显示每个候选的门禁状态、来源覆盖与缺口；`audit_agent_observations.py` 新增 gates_persisted / api_gates_match / gates_never_authorize_actions 检查。

## 本机端口共存迁移

本机 rag 项目占用 5433/8000、geomodel 占用 8010。经用户确认，本项目改为：控制面 API 18000、事故工作台 18010、业务库宿主端口 5434（容器内端口不变，`compose.override.yaml`）。`apps/lab_console/main.py`、`scripts/evaluate_agent_live.py`、`scripts/audit_agent_observations.py`、`scripts/probe_console_browser.py` 的端点改为环境变量可覆盖（`AEGISOPS_API_URL`/`AEGISOPS_CONSOLE_URL`，默认旧值）；`start_lab_ui.ps1` 与 README/lab README 更新为新端口。其余13个历史评估脚本仍硬编码旧端口，使用前需同样适配。

## 验证与真实证据

| 验证 | 结果和限制 |
| --- | --- |
| 定向测试 | 65个不同测试通过：root_cause_gate 8项（全/部分/未绑定/无claim/冲突/陈旧/纯函数/空）、agent_loop 新增4项契约（工具描述分类、提示契约、exact_fact_count、finish门禁持久化）、candidate_verifier 新增3项（告警上下文引用不阻塞、伪造引用失败关闭、仅上下文引用不能核验）。未跑全量套件 |
| 静态检查 | Ruff 通过；7个核心模块+4个脚本 mypy 通过（本机需 `MYPYPATH=src` 与新缓存目录）；JS/Compose 通过 |
| 库存故障真实复验 | `eval/agent-live-20261024T143944Z.json` 通过，事故 `1e601595-4543-4923-95ec-66c6a8b06763`：4模型/3工具，http_activity→**dependency_requests**（取得已核验依赖失败Span）→request_links（无样本）。较上批的实质进步：模型转向机制工具，claim 照抄工具身份（REQUEST_FAILURE/order-service/inventory-service/dependency_transport），不再为空。事故 `862c2a04` 上批曾用纯计数，本批同场景行为不同，证明不是脚本写死 |
| 验证器修正发现 | 该次运行候选1因同时引用告警上下文证据被判 MISSING_COMPLETED_TOOL_CITATION（INSUFFICIENT）。属验证器过严而非模型错误，已修正并补3项测试；修正后部署哈希与容器一致 |
| 缺遥测对照 | `eval/agent-live-20261024T144600Z.json` 通过，事故 `8f8768eb-b652-4c82-b772-ffe25da35c9f`，中性随机服务：4模型/3工具，无健康/根因定论，claim 均空，门禁 NO_VERIFIED_CLAIM |
| 最终审计 | `eval/agent-observation-audit-20261024T144649Z.json` 通过：双报告重读一致、核验/门禁持久化且 API 一致、引用完整、跨租户隐藏、匿名401、提案409、门禁恒不授权动作；后端探针用真实 `lab-gateway→order-service upstream_transport` Span 得 OBSERVATION_VERIFIED 且 UNBOUND/UNDETERMINED；部署源码哈希一致；结账恢复200；本批 18 次模型调用预留、返回 52672 tokens、0 缺失用量 |
| 浏览器视觉验收 | 无头 Edge CDP 390px 与 1440px 实测：两个事故详情均渲染"候选 N 根因门禁"行、缺口中文文案、限制汇总行；`document.scrollWidth==viewport` 无横向溢出；探针 DOM 断言门禁行存在。截图 `docs/screenshots/ops-root-cause-gate-inventory-20261024.png`、`ops-root-cause-gate-telemetry-gap-20261024.png`、`ops-root-cause-gate-desktop-20261024.png` |

## 边界与未完成

- 门禁通过≠根因定论≠可执行动作：本批聚合 Prometheus 告警均无直接来源绑定，所有真实候选门禁未通过，方向正确（如实暴露缺口）。
- 未做：中性名称的独立盲测（冻结真值与完整成员全集、与确定性基线同评分集）；审批修复闭环在自主报告下的验证；网络/数据库硬截止压力、真实 Temporal Activity 中断。
- 库存报告为验证器修正前生成，其候选1状态显示 INSUFFICIENT（机制事实未核验）；新调查将以修正后规则核验，无需重跑。
- 下一批：中性盲测 → 既有 Plan→OPA→Approval→Attempt→Recovery 闭环验证。
