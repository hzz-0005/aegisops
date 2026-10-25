# 真实模型联调候选

统一网关可接 Chat Completions 兼容接口，模型选择不锁定某家公司。当前预置六个**只读调查评估**档案；它们共用三份 Lab 证据、同一提示词和相同的本地结构及证据 ID 校验。DeepSeek 已完成真实联调；其余档案只核对过官方契约，尚未用真实密钥验证，不能把“有接口”视为“本项目已验收”。

| 档案 | 模型 | 本机密钥环境变量 | 官方依据 |
| --- | --- | --- | --- |
| `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` | [模型及接口](https://api-docs.deepseek.com/quick_start/pricing/)、[JSON 模式](https://api-docs.deepseek.com/guides/json_mode/) |
| `glm` | `glm-5` | `ZAI_API_KEY` | [Z.ai 结构化输出](https://docs.z.ai/guides/capabilities/struct-output)、[Chat Completions](https://docs.z.ai/api-reference/llm/chat-completion) |
| `kimi` | `kimi-k2.6` | `MOONSHOT_API_KEY` | [模型与接口](https://www.kimi.ai/help/kimi-api/api-overview)、[JSON 模式](https://www.kimi.ai/help/kimi-api/api-model-capabilities)、[开放平台地址](https://www.kimi.ai/help/kimi-code/cli-getting-started) |
| `mimo` | `mimo-v2.6-flash` | `MIMO_API_KEY` | [小米 MiMo 接口](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api)、[JSON 模式](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/text-generation/structured-output) |
| `qwen` | `qwen-plus` | `DASHSCOPE_API_KEY` | [阿里云兼容接口](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope)、[结构化输出](https://help.aliyun.com/en/model-studio/qwen-structured-output) |
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` | [OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs) |

```powershell
python -m scripts.evaluate_investigation --list-providers
# 先在当前 PowerShell 会话配置相应提供商的密钥，例如：
$env:MIMO_API_KEY = "<在本机填写>"
python -m scripts.evaluate_investigation --with-model --provider mimo
```

其他档案只需把 `mimo` 换成表内名称，并设置对应密钥。脚本不会打印密钥或模型正文；一次评估向选中的提供商发送三次最小化 Lab 证据请求，会产生 API 费用。Kimi 开放平台密钥与 Kimi Code 会员密钥不能混用。小米 Token Plan 若给出专用 Base URL，应改用下面的自定义模式，而非预置按量付费地址。

预置档案保留差异：DeepSeek 与小米 MiMo 关闭思考模式；Kimi、小米和 OpenAI 使用 `max_completion_tokens`；其余使用 `max_tokens`。网关统一要求 JSON 对象，并用 Pydantic 再校验字段和证据引用。JSON 模式不保证分析结论正确。模型版本和参数可能变化，实际联调失败时应按错误信息及当时官方文档调整。

新提供商不需要改 Incident Core。先用通用环境变量联调：

```powershell
$env:AEGISOPS_LLM_BASE_URL = "https://<提供商的 Chat Completions 根地址>"
$env:AEGISOPS_LLM_MODEL = "<模型 ID>"
$env:AEGISOPS_LLM_API_KEY = $env:MY_PROVIDER_API_KEY
$env:AEGISOPS_LLM_MAX_TOKENS_PARAMETER = "max_tokens"
$env:AEGISOPS_LLM_RESPONSE_FORMAT = "json_object"
python -m scripts.evaluate_investigation --with-model
```

确认协议差异及安全边界后，再决定是否增加预置档案。密钥只放本机环境或密钥管理器，不写入项目文件或聊天记录。生产数据外发仍需单独的数据分类、路由和保留策略。
