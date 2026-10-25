# 自主调查第二批：总结阶段与独立观测核验

2026-10-13，北京时间。计划见 `agent-plan-2026-10-13.md`。本批已部署、实测并恢复业务；独立根因定论仍未完成。

## 实现结果

- 最后一次模型调用进入 `FINAL_SUMMARY`，后台要求 `FINISH`，拒绝继续工具调用；工具预算耗尽也进入总结模式。没有增加原有4模型/8工具/90秒预算。正常路径现在最多3次取证后用第4次模型总结最后结果，模型也可提前结束。异常、重复查询、期限等仍可提前停止，不能保证所有异常都有模型总结。
- `ObservationClaim` 可表达精确的历史客户端请求失败或历史容器状态；自由文本根因保留为候选，可不提供结构化主张。模型只能提交候选和引用，不能提交后端验证结果。
- `candidate_verifier.py` 独立读取已完成的工具记录，核验后端租户/告警/目标/窗口范围、事实种类、来源、服务/依赖/失败码/Span身份、时间与引用。已知冲突即使模型没列出也参与检查；不同请求的不同失败码可并存，不能自动当作互相否定。
- 结果区分 `OBSERVATION_VERIFIED`、`CONFLICTED`、`INSUFFICIENT` 和 `UNSUPPORTED_CLAIM`。根因固定 `UNDETERMINED`、当前故障固定 `UNKNOWN`。这是观测核验契约与根因验证入口，**不是已实现通用根因验证器**，也不核验候选标题和自由文本推断。
- 既有只读直接来源查询加入 `request_membership` 工具。聚合Prometheus告警没有请求成员直接来源；有持久化来源时，显示完整来源分母与已核验分子，未覆盖成员保留。不把完整来源覆盖本身当成根因证明。
- 上游与下游客户端查询补 `upstream_http_5xx/upstream_transport`，仍限定登记的同租户调用方/依赖、固定±30秒窗口与真实Span核验。实际观测到的登记上游可被核验，但不能因此扩展模型可查询的目标权限。
- 核验结果存入既有报告的 `agent_trace` JSONB，API增加 `candidate_verifications`；无新增迁移。陈旧报告对外核验状态降为证据不足，保留历史轨迹。旧报告兼容默认空列表，旧账本不重置预算。
- 中文工作台显示核验状态、直接来源覆盖、根因/当前状态边界。未做本批视觉验收；JavaScript/API检查不能代替浏览器检查。

所有自主报告仍为 `INCONCLUSIVE`、无主根因、置信度0，不能进入动作提案。原确定性基线、审批/执行目录未扩展。

## 验证与真实证据

| 验证 | 结果和限制 |
| --- | --- |
| 定向测试 | 85个不同测试通过：先运行84个，新增上游权限核验反例后重跑受影响50个；服务读取修改后API/来源测试20个通过。包括正例、跨租户/告警/目标、时间过期、身份/伪引用、未完成工具、冲突、混合请求码、重复成员、1/2来源覆盖、模型总结与旧基线。未跑全量套件 |
| 静态检查 | 修改文件Ruff通过；7个核心模块/脚本mypy通过，新增审计脚本单独mypy通过；JS语法与Compose解析通过 |
| 真实PostgreSQL | `python -m scripts.verify_agent_journal` 通过并发预留、中断未知结果、旧所有者、租户和版本门禁；仅清理自身新建UUID，零模型调用。不是实际Temporal进程中断测试 |
| 库存退出开发复验 | `eval/agent-live-20261013T032129Z.json` 通过，事故 `862c2a04-ad01-4cb8-82be-9b170e2c3300`：4模型/3工具，HTTP统计→日志NO_DATA→错误Span计数，第4轮FINISH总结最后结果。两轮有实际观察，但未识别具体失败依赖或机制，候选claim均为空，核验返回UNSUPPORTED_CLAIM。库存和结账恢复 |
| 首次缺遥测对照 | `eval/agent-live-20261013T032247Z.json` 保留：4模型/3工具均NO_DATA后FINISH，无健康/根因定论。服务名telemetry-gap-control泄漏测试含义，模型引用名字推断；不能视为独立盲测 |
| 修正后的中性对照 | `eval/agent-live-20261013T032615Z.json` 通过，事故 `f21001a0-0cc7-4739-9e7d-d3a1886b4627`，中性随机服务observed-service-11d1fb79：HTTP/日志/探针均NO_DATA，4模型/3工具，FINISH明确无健康证明、根因未知、尚有未查询工具。没有注入真实故障 |
| 最终API/部署/核验审计 | `eval/agent-observation-audit-20261013T032802Z.json` 通过；三报告重读/核验持久化/API一致、引用存在、跨租户隐藏、匿名401、提案409；工作流源码哈希与本机一致。API/工作台/结账200、0活动告警、本轮0进行中账本 |

审计额外用新上游工具取得真实 `lab-gateway → order-service upstream_transport` Span，后端探测主张返回 `OBSERVATION_VERIFIED`，同时 `UNBOUND/UNDETERMINED/UNKNOWN`。字段名 `backend_verifier_probe_not_model_candidate` 明确它是适配器和验证器实测，不是模型自己提出或确认的根因。直接来源完整覆盖本批仅有单元反例/正例验证，没有新的真实来源窗口完整覆盖演示。

本批真实复验是开发验收，不是独立盲测。没有根因准确率、分组改善百分比或审批修复成功率。模型本次取证仍停在现象统计，不能以测试通过代替诊断能力。

从首次本批真实复验开始，包含其他事故与刷新：20次模型调用预留，返回 `total_tokens` 共49930，0次缺失返回用量；未计算货币费用。旧具名对照与中性重跑都计入，不能只算选中的两条报告。

运行本项目24容器，另12个docker项目容器保留。API/workflow-worker镜像重建，数据库仍0026。工作台普通Uvicorn进程PID25520/执行会话66087，地址 `http://127.0.0.1:8010/?view=ops`。后台启动命令曾被自动审批以blocked by policy拒绝，普通前台开发服务启动成功。未修改密钥、提交或推送；主体源码仍Git未跟踪。

## 继续工作

1. 提高模型机制取证质量：主要故障报告本次只用了请求/错误Span计数，结构化主张为空。让模型理解计数工具与机制工具的证明范围，在有限阶段中补最关键缺口；不能用固定答案目录或强行填写claim替代自主推理。
2. 补实际机制证明规则与独立验证所需事实，区分确切历史观测、请求症状、传播路径、来源全覆盖、当前故障与根因。数据库身份/阻塞观测按实际事故缺口补。只有满足机制与范围门禁的结论才讨论SUPPORTED及动作映射。
3. 冻结独立盲测输入/真值及完整成员全集，和原确定性基线用同一评分集合；名称中性，真值不进模型，未知不算正确。当前实测和后端探测不得计为盲测准确率。
4. 完成后接既有Plan→OPA→Approval→Attempt→Recovery；本批没有通过审批执行修复，库存恢复是评估脚本finally清理。
5. 网络/数据库硬截止压力、真实Temporal Activity中断、视觉验收仍未完成。外部测试/预发布环境未提供，P4未开始。

复用命令（真实复验会消耗现有模型额度，第一个会停止并恢复本项目库存）：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_agent_live --case inventory-unavailable
.\.venv\Scripts\python.exe -m scripts.evaluate_agent_live --case missing-telemetry
.\.venv\Scripts\python.exe -m scripts.audit_agent_observations --reports eval/agent-live-20261013T032129Z.json eval/agent-live-20261013T032247Z.json eval/agent-live-20261013T032615Z.json
```
