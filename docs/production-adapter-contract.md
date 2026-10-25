# 生产适配边界（草案 v1）

当前运行物是开发 Lab。生产接入时保留 `Alert → Correlation → Incident → Investigation → Plan → Policy → Approval → Attempt → Verification` 的账本和版本栅栏；替换业务信号来源、身份与执行适配器。不能把 Compose 的 Docker Socket、固定容器名、`bad_deployment` 实验标记或开发令牌带入生产。

## 目标身份

每次提议和执行必须绑定 `(tenant_id, environment, target_kind, catalog_id, catalog_version)`。`slug` 只供查找与展示，不作为执行身份。适配器还要记录所管理平台的不可变目标 ID（例如集群 UID 与工作负载 UID）；执行前重新读取平台 ID，与获批快照比对。环境必须来自持久化目录，不允许由执行器自行填一个 `lab` 或 `production` 字符串。

`ActionSnapshot` 已支持版本化 v2：在 `target=kind/UUID` 和 `resource_version` 外，强制绑定租户 ID、环境和平台目标 ID，并将它们纳入摘要。历史 v1 的六字段摘要保持不变；Lab 流程在提议、策略查询和执行三处限定 `environment=lab`，OPA 与 Lab 执行器均拒绝 v2。v2 当前只是身份契约，还没有生产计划生成、生产策略或执行适配器，不能用它绕过开发门槛。

## 发布身份

生产发布事件需要同时记录变更来源、变更 ID、目标工作负载和前后两个不可变版本：制品摘要、配置摘要、发布控制器的 revision/UID、观察时间与证据来源。`from_ref`/`to_ref` 可以供人阅读，但不能单独证明回滚目标。前后版本应来自发布平台的可信 API，执行时重读当前版本并比较；出现漂移则不执行。

现有 `DeploymentFingerprint` 是 Lab 格式（Docker image ID、Compose 配置哈希、实验 `bad_deployment` 标记），只服务于同镜像 Order 基线回滚。生产契约应把“是否故障”留在调查证据中，不能当成发布身份字段；配置摘要应覆盖实际部署规格，不能沿用 Compose 哈希。

## 执行和核验

生产适配器的最小操作顺序：

1. 以租户和环境作用域读取目标、当前版本、健康状态及平台操作权限。
2. 比较快照中的目标 ID、当前版本、审批、OPA 决策、Incident 版本、租约和 fencing token；任一不符则拒绝。
3. 提交一个带 Action/Attempt 幂等标识的有限操作（例如发布平台“回滚到已批准 revision”）；不接受模型生成的命令、镜像或参数。
4. 记录平台操作 ID 和响应；响应丢失时进入人工核对，不能自动重试副作用。
5. 独立读取目标版本与业务健康信号；区分“目标版本已观察到”和“业务恢复已观察到”，不把时间先后当成动作因果证明。

生产 Payment/Order 接入还需定义受保护的只读探针和指标、支付交易的脱敏与保留规则、幂等业务请求及沙箱验收。控制面数据库和业务数据库继续隔离。

## 启用门槛

当前 API 仍使用开发租户 ID 和静态开发令牌，OPA `EXECUTE` 只允许 Lab，Lab 执行器只允许 `dev` profile。API、事件 Worker 和 Temporal 进程现在也统一拒绝非 `dev` profile 启动。这些是明确的未完成项。启用生产前必须完成真实身份与租户解析、生产 OPA 策略、最小权限凭证、独立生产执行器、平台版本观察、业务探针、故障注入与审计验收。只设置 `AEGISOPS_PROFILE=production` 或接入真实支付 API 不构成生产可用。
