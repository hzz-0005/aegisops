# Investigation Replay 评估

`investigation_cases.json` 是从三起真实 Lab 事故的持久化调查报告导出的固定样本：Redis 不可用、订单服务数据库连接池耗尽、订单服务错误部署。每个样本保留报告 ID、预期根因、状态和模型复核实际会收到的最小化结构化输入。它不包含原始日志或追踪正文；报告限制项已转换成固定类别，不外发其中的内部标识。

在项目根目录运行：

```powershell
python -m scripts.evaluate_investigation
```

离线检查要求每种场景恰好一份样本、报告 ID 一致、证据 ID 唯一、事实字段属于白名单、输入未超预算，且确定性报告的根因与场景预期一致。更新 Lab 报告后可运行 `python -m scripts.export_investigation_eval` 重新导出，再运行离线检查。导出需要本地 Lab 数据库；离线检查只读取此 JSON 文件。

配置 `AEGISOPS_LLM_BASE_URL`、`AEGISOPS_LLM_MODEL` 和 `AEGISOPS_LLM_API_KEY` 后，可以运行：

```powershell
python -m scripts.evaluate_investigation --with-model
```

该命令对三份样本各发起一次只读模型复核，检查结构化输出和证据引用范围，并记录调用耗时；它不写数据库，也不触发任何处置动作。命令不打印模型正文。外部调用会产生提供商费用，且结构化输入会发给配置的提供商。

六个候选提供商的档案与配置见 [真实模型联调候选](model-providers.md)。可运行 `python -m scripts.evaluate_investigation --list-providers` 查看；`--with-model --provider glm` 等命令选择单一档案。默认请求 `response_format={"type":"json_object"}`，本地继续检查输出模型和证据引用。

这三个样本检验的是已有确定性调查和模型引用边界，不能证明新故障的根因识别率。模型输入本身包含确定性假设，因此 `--with-model` 也不是盲测；通过引用检查不等于模型解释正确。当前样本数量少，且部分历史报告在 Incident 解决后已过期，只用于回放，不应用于当前事故决策。生产数据进入外部模型前仍需数据分类、路由和保留策略。
