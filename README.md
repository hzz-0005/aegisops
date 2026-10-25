# AegisOps

**受治理的自主事故响应原型**：LLM Agent 在真实 Docker 微服务实验环境里自主调查注入的故障——用证据核验的主张、独立的根因门禁和人工审批的受控修复，而不是让模型自由发挥。

> **核心原则：没有任何 LLM 生成的任意命令可以触碰受管理的生产资源。**
> 调查只读；修复动作来自人工审阅的映射表；执行前过 OPA 策略 + 独立审批 + 前置条件复读。

[[_测评结果](#盲测结果)] · [[快速开始](#快速开始)] · [[架构](#架构)] · [[诚实的能力边界](#诚实的能力边界)]

## 这是什么

一个运行在本地 Docker Lab（网关/订单/库存/支付 + Redis + PostgreSQL + 完整遥测链）上的事故响应系统：

1. **真实故障注入**（容器暂停/停止、缓存不可用、连接池耗尽、坏部署…）产生真实告警、指标、日志、追踪和请求失败事件
2. **自主调查**：模型在有限预算内（4 次模型调用/8 次工具/90 秒）自主选择只读工具取证、更新候选假设、提交**结构化可核验主张**（claim）
3. **独立验证**：后端验证器逐条核验 claim 的身份/时间/来源（模型说了不算）；根因门禁要求「机制已核验 × 告警直接来源全覆盖」双满足
4. **受治理修复**：门禁通过的候选经确定性映射表进入方案 → OPA 策略 → 独立人工审批 → fencing 执行器（前置条件复读）→ 恢复核验 → 结案
5. **全过程审计**：每个决定持久化，事故时间线可回放

```text
真实故障 → 告警/信号 → 自主调查(只读工具+预算) → 观测核验 → 根因门禁
                                                        ↓ 通过
     结案 ← 恢复核验 ← 受控执行 ← 独立审批 ← OPA ← 映射表 ← 修复方案
```

## 盲测结果

冻结清单（`eval/agent-blind-manifest-v2.json`）、真值与模型输入严格分离、
事后评分（方法学锚定 [RCAEval](https://github.com/phamquiluan/RCAEval) 与
[AIOpsLab](https://github.com/microsoft/AIOpsLab)）、未知不算正确：

| 指标 | 结果（当前配置，6 故障案例 + 1 对照 × 1 次） |
|---|---|
| 定位正确（已核验 claim 指向真值根因服务） | **4/6 = 0.667**（随机基线 0.2，Lift ≈ 3.3）|
| 机制精确（再匹配注入机制身份） | 2/6 |
| 错误主张 | **0** |
| 范围受限案例（真值在只读工具范围外） | 2/6，均为诚实出口、零错误主张 |
| 安静对照 | 零误报 |
| 成本 | ~11k tokens/例 |

完整分母与逐例记录：`eval/agent-blind-summary-*.json`。样本量小、每案例 1 次，
**不构成通用诊断能力宣称**。方法与边界详见
[docs/agent-blind-eval-2026-10-24.md](docs/agent-blind-eval-2026-10-24.md)。

端到端治理闭环（真实 Redis 故障走完全链）已验证：
[docs/agent-remediation-loop-2026-10-25.md](docs/agent-remediation-loop-2026-10-25.md)。

## 快速开始

需要：Python 3.12+、Docker Desktop。本机端口约定：API `18000`、工作台 `18010`、
业务库宿主 `5434`（见 `compose.override.yaml`，避免与同机其他项目冲突，可改）。

```powershell
git clone <this-repo> && cd aegisops
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
docker compose up -d          # 24 个容器：业务 Lab + 控制面 + 遥测
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18000
# 另一个终端：
.\.venv\Scripts\python -m uvicorn apps.lab_console.main:app --host 127.0.0.1 --port 18010
```

- 商城与事故工作台：<http://127.0.0.1:18010/> · <http://127.0.0.1:18010/?view=ops>
- 无模型密钥也能跑：Agent 优雅退出，确定性规则基线照常调查
- 配置 LLM（任意兼容 Chat Completions 的端点）：复制 `.env.example` 为 `.env` 填入密钥
- 一键演示：`./demo.sh`（注入故障 → 信号提升 → 自主调查 → 门禁 → 审批 → 执行 → 恢复 → 结案）

## 架构

```text
apps/            API（FastAPI）· 工作台（本机控制台）· Temporal worker · 独立 lab-executor
src/aegisops/    领域模型 · 关联引擎 · 自主调查循环（agent_loop/验证器/根因门禁）
                 修复治理（OPA 客户端/审批/fencing 执行/恢复评估）· 请求信号检测器
lab/             业务微服务（网关/订单/库存/支付）+ 故障注入场景
infra/opa/       remediation-policy（Rego，附测试）
migrations/      27 个 Alembic 迁移（无启动时 create_all）
```

**治理链的关键设计**：

- **调查 Activity 内嵌 Agent 循环**（LLM 不进确定性 Workflow 代码）；数据库 journal
  先预留后调用，中断不清零预算、不重复付费调用（真实 kill worker 已验证）
- **验证器只认后端记录**：claim 必须匹配已完成工具事实的身份/时间/来源；
  模型声明的冲突仅审计；伪造引用失败关闭
- **执行器与控制面分离**：唯一持有 Docker Socket 的容器；执行前复读 OPA/审批/
  快照哈希/真实故障前置条件；结果不确定进入人工核对而非盲目重试
- **非 dev 环境启动即拒绝**（真实身份/租户体系是 P4 前置，见
  [docs/production-adapter-contract.md](docs/production-adapter-contract.md)）

## 验证

```powershell
.\.venv\Scripts\python -m pytest              # 351 项测试
.\.venv\Scripts\python -m ruff check .
docker compose run --rm --no-deps opa test /policy
```

韧性已验证：Temporal Activity 真实中断（诚实失败 + journal 到期接管）、
执行结果不确定隔离、并发预留栅栏、跨租户隔离。

## 诚实的能力边界

- 已验证的执行适配器只有本机 Lab 三类（Redis 重启/订单重启/订单回滚）；
  映射表目前只有一条（Redis 缓存）——**扩张需逐条评审，模型永不直接产出动作**
- 盲测样本量小（见上）；分组关联、请求传播分组仍在研究，不自动并案
- 固定开发租户/静态令牌；无生产身份、租户隔离、真实支付/发布接入
- 本项目是**本地 Lab 原型**，不是生产系统；生产适配边界见上述契约文档

## 许可

Apache-2.0。见 [LICENSE](LICENSE)。
