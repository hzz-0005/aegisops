# Enterprise Lab：业务故障场景

这是 AegisOps 的可复现微服务实验环境，只使用模拟商品和模拟付款，不连接真实支付渠道。

```text
POST /checkout → Lab Gateway → Order Service → Inventory Service → Lab Orders PostgreSQL
                                      ↓
                                Payment Service → Redis（缓存依赖）
                                      ↓
                                  Lab Orders PostgreSQL（付款事实）

四个服务 → OpenTelemetry Collector → Prometheus / Loki / Tempo → Grafana
                                      Prometheus → Alertmanager → AegisOps API
                                      → AegisOps PostgreSQL Outbox → RabbitMQ → Inbox → Incident
```

## 启动与验收

页面入口：在完整 Compose 环境就绪后，从仓库根目录运行 `python -m uvicorn apps.lab_console.main:app --host 127.0.0.1 --port 18010`，打开 `http://127.0.0.1:18010/`。商城的四件商品来自 Inventory，结账经过 Gateway、Order、Payment 和独立业务库。事故控制台可运行 Redis、订单数据库连接池、错误部署三个真实故障脚本，也可向通用告警入口发送自定义**模拟**告警。事故详情展示调查证据、限制、模型复核、修复建议及审批；目录外根因不会生成可执行动作。当前页面与静态开发令牌只限本机 Dev profile。

在仓库根目录运行：

```powershell
docker compose up -d --build
.\.venv\Scripts\python -m lab.bootstrap_catalog
.\.venv\Scripts\python lab\scenarios\redis_down.py
.\.venv\Scripts\python -m lab.scenarios.inventory_down
.\.venv\Scripts\python lab\scenarios\alert_pipeline.py
.\.venv\Scripts\python -m lab.scenarios.multi_fault_correlation
.\.venv\Scripts\python -m lab.scenarios.correlation_conflicts
.\.venv\Scripts\python -m lab.scenarios.incident_corrections
.\.venv\Scripts\python -m lab.scenarios.replay_harness
.\.venv\Scripts\python -m lab.scenarios.delivery_reliability
.\.venv\Scripts\python -m lab.scenarios.db_pool_exhaustion
.\.venv\Scripts\python -m lab.scenarios.bad_deployment
.\.venv\Scripts\python -m lab.scenarios.three_scenario_replay
.\.venv\Scripts\python -m lab.scenarios.worker_crash_replay
```

验收脚本会先完成正常下单和重复付款检查，只停止本项目的 Redis 容器。它会验证 Payment 500、Checkout 502、本次新增的故障指标、Prometheus 告警、Loki 错误日志和 Tempo 中的失败 Redis span，最后无论成功失败都会重新启动 Redis，并检查恢复后的下单。脚本需要本地开发环境已安装 `httpx`（`pip install -e ".[dev]"`）。

第二个脚本验证告警 webhook 的鉴权与去重、Outbox 发布、RabbitMQ 消费后的 Inbox 记录、两条同服务告警关联到一个 Incident，以及 Incident 查询和时间线。真实 Redis Down 验收期间，Alertmanager 也会将 `PaymentRedisUnavailable` 送到同一条控制面链路。开发环境读取 Incident API 需要请求头 `Authorization: Bearer aegisops_dev_only`；这是本机演示令牌。

`inventory_down.py` 真实停止库存服务，验证订单失败、新的 `OrderInventoryUnavailable` 告警与独立 Incident，结束时恢复服务。`multi_fault_correlation.py` 向 webhook 注入六条模拟告警，分别代表缓存故障、数据库连接耗尽和错误部署，验证每组两条相关告警合为一个 Incident、三组互不误合并。连接池耗尽和错误部署另有真实业务服务故障场景。人工取消和升级另用本机操作令牌 `Bearer aegisops_operator_dev_only`，与告警令牌分开。

`bootstrap_catalog.py` 把当前 Lab 的服务、资源和依赖登记到控制面，重复运行不会重复登记。`correlation_conflicts.py` 验证同一资源上不同部署变更不会被强制合并，数据库同时保存支持与冲突特征、得分和规则版本。关联规则仍需后续真实场景及回放验证。

`incident_corrections.py` 验证带版本和原因的人工拆分、合并、关系及双侧审计时间线；拆分后同时匹配两侧的新告警会建立独立事故。`replay_harness.py` 以告警集合形成的 Incident Partition 比较正常、逆序与重复上报后的结果，不比较随机 Incident ID。`delivery_reliability.py` 验证 worker 重启后的 Outbox 补处理、RabbitMQ 重投幂等和 20 条并发告警分组。`db_pool_exhaustion.py` 使用本机开发令牌占满订单服务的真实有界数据库连接池，并验证结账 503、恢复、Prometheus 指标和控制面可用。

`bad_deployment.py` 使用 Compose 覆盖文件实际重建带故障配置的 Order 容器，登记 `ChangeEvent`，验证 Order 与 Gateway 的两条真实告警归入同一 Incident，并在 `finally` 中回滚容器。`three_scenario_replay.py` 依次运行 Redis、连接池和坏部署三个真实场景，检查根因告警形成三个不同 Incident。`worker_crash_replay.py` 在开发环境中让 worker 于数据库提交后、RabbitMQ 确认前退出，随后恢复 worker，验证消息重投且数据库效果只有一次。以上脚本会修改本机 Lab 容器状态，完成后自动恢复故障服务。

业务表现在位于独立的 `lab-orders-postgres`（容器内 5432；宿主端口经 `compose.override.yaml` 映射为 5434，避免与本机其他项目冲突），由 `lab-migrate` 的独立迁移管理。控制面仍在 `aegisops-postgres`（本机 5432 端口）；旧库里的 `lab` schema 是隔离前的历史测试数据，现有业务服务已不再读取它。

开发环境可用操作令牌调用 `POST /api/v1/change-events` 登记变更；同一 `source` 与 `external_id` 重试返回同一记录。`GET /api/v1/incidents/{id}/correlation-decisions` 用告警令牌查看自动关联依据。实验中的模拟变更记录不代表真实部署已经发生。

本机入口：

| 用途 | 地址 |
| --- | --- |
| 实验网关 | `http://127.0.0.1:8001/docs` |
| 控制面 API | `http://127.0.0.1:18000/docs` |
| Grafana | `http://127.0.0.1:3000` |
| Prometheus | `http://127.0.0.1:9090` |
| Alertmanager | `http://127.0.0.1:9093` |
| RabbitMQ 管理页 | `http://127.0.0.1:15672` |
| Loki | `http://127.0.0.1:3100` |
| Tempo | `http://127.0.0.1:3200` |

Grafana 的本机开发账号为 `admin` / `aegisops_dev_only`，三个数据源已预配置。Prometheus 可查询 `lab_payment_redis_failures_total` 和 `lab_http_requests_total`；告警名为 `PaymentRedisUnavailable`。告警使用最近一分钟的新增失败，因此 Redis 恢复后会随时间窗口结束而清除。

## 支付替换边界

`POST /checkout` 必须包含客户端生成并在重试时复用的 UUID `idempotency_key`，例如 `{"idempotency_key":"<UUID>","sku":"sku-demo","quantity":1}`。Order 以该键保存唯一订单；同键不同商品或数量返回 409。付款结果不确定时，同键重试复用原订单 ID 与原金额，不再建立第二笔付款。`PaymentIntent` 包含订单 ID（提供者幂等键）、金额和币种；`PaymentReceipt` 返回本地付款 ID 和 `SUCCEEDED`、`PENDING` 或 `FAILED` 状态。Order 只依赖这些契约，不依赖模拟付款的数据库实现。当前 `SimulatedPostgresPaymentProvider` 实现 `PaymentProvider` 接口，付款事实写入 PostgreSQL，Redis 只作缓存依赖。

运行 `python -m scripts.verify_checkout_idempotency` 可检查必填键、重复请求、参数冲突，以及模拟“付款已提交但订单回复丢失”后的同键恢复。该脚本只操作 Lab 业务库，并在结束时恢复测试订单状态。

未来接入真实支付渠道时，在 Payment Service 中实现新的 `PaymentProvider`，并加入凭证管理、渠道幂等键、回调签名验证、异步状态更新、对账和异常补偿。真实支付的适配与审核是单独的交付工作；当前代码不会触发真实扣款。Order 已能接收 `PENDING` 状态，后续需增加由可信回调驱动的状态更新。客户端必须持久保存同一业务操作的 `idempotency_key`；每次重试生成新键仍可能产生重复订单。

## 当前实验边界

- 库存只查询可用量，尚未做预留或扣减；此场景验证的是 Redis 故障对支付的影响。
- Redis 读取失败会让 Payment 明确报错，以便故障可观测；实际业务是否允许降级须由产品与风险策略决定。
- Prometheus、Loki、Tempo 当前使用本地临时存储，容器重建后历史遥测可能丢失。
- 控制面已接入统一告警、Incident 关联、时间线、人工取消、升级、合并与拆分。Incident API 分别返回 `status` 与 `processing_phase`；关联完成后的新事故为 `OPEN / READY_FOR_INVESTIGATION`。关联决定记录支持与冲突证据；乱序、重复投递、提交后崩溃及一组并发告警已实测。只有 `FIRING` 告警参与新事故候选搜索，已 `RESOLVED` 的旧告警不会再次拉入新根因。`incident.created.v1` 经 Outbox、RabbitMQ 进入 Temporal；只读调查、受控 Lab 执行和结案已对 Redis、订单连接池与错误部署场景验收。真实模型复核仍待 API Key 联调；生产身份、发布平台和支付渠道尚未接入。
- 订单服务的连接池故障注入接口只在配置 `LAB_FAULT_TOKEN` 时可调用，实验环境的令牌为 `aegisops_lab_fault_dev_only`。真实部署时不应配置此开发令牌。
