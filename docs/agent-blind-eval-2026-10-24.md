# 自主调查第四批：中性盲测（冻结清单+独立真值+确定性对照）

2026-10-24，北京时间。接续 `agent-mechanism-gate-2026-10-24.md`。本批建立并执行了首个中性盲测批次；样本量小，不构成通用能力宣称。审批修复闭环仍待下批。

## 方法来源（避免闭门造车）

- [RCAEval](https://github.com/phamquiluan/RCAEval)（FSE 2025，[arXiv:2412.17015](https://arxiv.org/html/2412.17015v5)）：真值（根因服务+指标）与输入分离存放，被测方法只接收遥测与异常时间戳，harness 事后对照评分；报告精确率外同时给出随机猜测基线。
- [AIOpsLab](https://github.com/microsoft/AIOpsLab)（微软，[arXiv:2501.06706](https://arxiv.org/pdf/2501.06706)）：正确性按检测/定位/分析/解决分层对照注入真值；效率按耗时与步数。

本批采纳：真值分离（模型只见中性告警）、事后评分、两层正确性（定位/机制精确）、随机基线 Lift、效率指标（模型轮次/工具轮次/耗时/tokens）。

## 冻结设计

清单 `eval/agent-blind-manifest-v1.json`（version=agent-blind-manifest-v1，案例ID为固定UUID）：

| case | 注入 | 真值根因服务 | 机制 | 性质 |
| --- | --- | --- | --- | --- |
| ec9600c1 | pause inventory-service | inventory-service | process_paused | 同依赖不同机制之一 |
| bb720e1f | stop inventory-service | inventory-service | process_exited | 同依赖不同机制之二 |
| fb9dbba2 | pause lab-gateway | lab-gateway | process_paused | 范围受限（工具无法从 order 告警范围观测网关状态） |
| b4064db5 | 无 | — | — | 安静对照 |

- 中性告警：所有故障案例统一 `OperationalObservation`/order-service（alert 名、服务、摘要完全一致，不随案例变化）；对照用随机中性服务。注入器复用既有 `evaluate_fault_battery` 机制（compose pause/stop），故障名/脚本名/真值只存在于清单，仅被 harness 用于注入与事后评分。
- 评分两层：**定位正确**=后端已核验 claim（OBSERVATION_VERIFIED）的 service/dependency 指向真值根因服务；**机制精确**=再匹配冻结的机制身份（paused/exited/失败码族）。未知不算正确；验证过但指向其他服务的 claim 记 WRONG；对照出现任何已核验 claim 或 confidence>0 记 FALSE_POSITIVE；范围受限案例无已核验 claim 记 SCOPE_LIMITED_UNKNOWN。每例同时检查修复提议 409（不可执行栅栏）。
- 随机基线：plausible_root_services=5（order 告警可claim的服务面，冻结于清单，非来自真值），Chance@1=0.2。
- 评分器纯函数独立成模块 `scripts/agent_blind_scoring.py`，10项单元测试（含"准确率分子不得计入对照"缺陷的回归）。
- harness `scripts/evaluate_agent_blind.py`：基线结账→等安静→注入→核验业务症状→中性告警→有界等待报告→finally恢复→读报告评分；记录工具序列、token、部署的 Agent 开关（docker exec 实读 Settings，非假设）；输出 eval/agent-blind-<ts>.json。

## 执行结果

| 批次 | 配置 | 定位正确 | 机制精确 | 错误 | 未知/范围受限 | 对照 | Lift |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `eval/agent-blind-20261024T152105Z.json` | v3 提示 | 1/3 | 1 | 0 | 1/1 | 干净 | 1.67 |
| `eval/agent-blind-20261024T153126Z.json` | **v4 提示（主要结果）** | **2/3** | **1** | **0** | 0/1 | 干净 | **3.33** |
| `eval/agent-blind-20261024T154433Z.json` | 确定性基线（同清单，Agent关闭实测） | 0/3 | 0 | 0 | 3/0 | 干净 | 0 |

- v3→v4 之间盲测暴露的真实缺陷：库存暂停案例已取得精确事实（summary 准确描述 dependency_timeout）但最后总结轮未把 claim 填上假设，核验无效。修复=提示 `readonly-planner-v4-zh` 增加"每轮输出假设；引用证据含精确事实必须补全 claim，不得只文字描述"；无预算变化。v4 同案例复跑即定位正确（claim REQUEST_FAILURE order→inventory dependency_timeout 已核验），证明修复有效且非脚本写死。
- 库存停止案例两批均定位+机制双正确（v4 中 dependency_requests 为第一个工具）；库存暂停案例 v4 定位正确但机制未精确（模型未调 runtime_state 观测 paused 状态）——真实能力边界，如实报告。
- 网关暂停案例两批均 SCOPE_LIMITED_UNKNOWN 且无错误 claim（v4 中一次 RUNTIME_STATE claim 被核验器判 CONFLICTED 拒绝）——范围受限受控出口有效；这是 P1 补遥测的明确缺口（登记上游的运行时状态观测）。
- 确定性基线（同清单、`AEGISOPS_INVESTIGATION_AGENT_ENABLED=false` 实测后恢复）：中性告警全部如实 INCONCLUSIVE、无根因主张、0错误——它本就依赖信号目录，该对照量化了 Agent 循环在目录外告警上的增量，不构成"基线差"的一般结论。
- 治理栅栏：全部案例修复提议 409；对照无误报。
- 成本：v3 批 45360 tokens、v4 批 46357 tokens（DeepSeek，4模型/3工具/例）、确定性批 0；本会话累计真实模型消耗约 14.4 万 tokens（含上午复验 52672）。

## 验证与视觉验收

- 75个定向测试通过（含评分器10项）；Ruff/mypy（MYPYPATH=src+新缓存）/JS/Compose 通过；v4 提示与门禁代码部署哈希一致。
- 内置浏览器（IAB，见下节）与无头 Edge 双路验证：盲测事故详情正确渲染已核验依赖请求、候选核验行、根因门禁行（缺口只剩"来源未绑定+当前状态未知"）；390px 无横向溢出。截图：`ops-blind-verified-claim-20261024.png`（1440px 无头）、`ops-iab-blind-verified-claim-20261024.png`（内置浏览器实测存档）。

## node_repl 运行时配置（环境级，非仓库）

Browser Use / Computer Use 的 `mcp__node_repl__js` 由官方 `node-repl-host` 插件提供，但本机 `installed_plugins.json` 未记录该插件，导致会话缺工具。已在用户级 `~/.zcode/cli/config.json` 增加 `mcp.servers.node_repl`（stdio：`node <cache>/node-repl-host/0.6.1/dist/mcp/server.js`，env 指向 browser-use/computer-use 缓存根），重启会话后生效并实测连通（IAB 打开事故工作台、读取门禁 DOM、截图均成功）。

## 边界与未完成

- 样本量：3个故障案例+对照，单一清单版本。这是盲测协议的首跑，不是通用诊断能力；扩大故障集（陌生机制/混合根因/缺失证据）按计划继续，且必须同评分集合比较。
- 机制精确率仅 1/3：定位≠机制；runtime_state 未被选用的行为模式值得下批观察（是否需要证据面提示之外的引导，不能用固定目录替代推理）。
- 范围受限案例暴露的登记上游运行时观测缺口未补（P1精神：按实际调查缺口补）。
- 审批修复闭环（Plan→OPA→Approval→Attempt→Recovery 在自主报告下的验证，含"当前故障状态"维度设计）未开始，是下一批主体。
