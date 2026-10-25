# 贡献指南 / Contributing

感谢关注 AegisOps。这是一个研究型原型项目：受治理的自主事故响应（本地 Lab 验证）。

## 开发环境

需要 Python 3.12+、Docker Desktop。详见 [README](README.md) 的快速开始。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
docker compose up -d
.\.venv\Scripts\python -m alembic upgrade head
```

注意本机端口约定：控制面 API `18000`、事故工作台 `18010`、业务库宿主 `5434`
（避免与同机其他项目冲突；见 `compose.override.yaml`，可按需修改）。

## 提交前检查

改动必须通过（按影响范围裁剪，不必全跑）：

```powershell
.\.venv\Scripts\python -m pytest tests/<相关文件> -q
.\.venv\Scripts\python -m ruff check .
MYPYPATH=src .\.venv\Scripts\python -m mypy <改动模块>
docker compose config
```

涉及治理链路（方案/OPA/审批/执行）的改动必须附真实 Lab 运行证据
（eval 文件路径 + 通过/失败如实记录），不接受仅单元测试的合入。

## 项目不变量（贡献者必读）

这些边界是项目的设计核心，PR 不得绕过：

1. **调查阶段只读**：无写工具、无 Docker Socket、无任意 Shell。
2. **模型不产出动作参数**：修复动作只能来自已审阅的确定性映射表/信号目录。
3. **独立验证**：模型候选必须过后端验证器（观测核验）与根因门禁
   （机制×来源覆盖），模型声明的冲突只有审计意义。
4. **未知不算正确**：评估口径见 `scripts/agent_blind_scoring.py`；
   盲测真值与模型输入严格分离。
5. **执行走既有治理链**：Plan → OPA → 独立审批 → fencing 执行，
   不新增绕过路径。
6. **诚实声明**：能力宣称必须有分母、有证据路径；未知/范围受限如实报告。

## 提交信息

用一句话说清"做了什么 + 验证方式"，例如：
`Verifier: model-declared conflicts are advisory only; regression test added`

## 报告问题

请附：复现步骤、相关 eval 报告文件名、docker compose ps 输出。
安全问题请勿开公开 issue。
