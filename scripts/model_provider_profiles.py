"""Optional, read-only evaluation presets for official Chat Completions APIs."""

import os
from dataclasses import dataclass
from typing import Literal

from aegisops.config import Settings


@dataclass(frozen=True)
class ProviderProfile:
    base_url: str
    model: str
    key_env: str
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    thinking_mode: Literal["default", "disabled"] = "default"
    max_output_tokens: int = 1200
    timeout_seconds: float = 60


PROVIDERS = {
    "deepseek": ProviderProfile(
        "https://api.deepseek.com",
        "deepseek-flash",
        "DEEPSEEK_API_KEY",
        thinking_mode="disabled",
        max_output_tokens=500,
    ),
    "glm": ProviderProfile(
        "https://api.z.ai/api/paas/v4",
        "glm-5",
        "ZAI_API_KEY",
    ),
    "kimi": ProviderProfile(
        "https://api.moonshot.cn/v1",
        "kimi-k2.6",
        "MOONSHOT_API_KEY",
        token_parameter="max_completion_tokens",
    ),
    "mimo": ProviderProfile(
        "https://api.xiaomimimo.com/v1",
        "mimo-v2.6-flash",
        "MIMO_API_KEY",
        token_parameter="max_completion_tokens",
        thinking_mode="disabled",
    ),
    "qwen": ProviderProfile(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "DASHSCOPE_API_KEY",
    ),
    "openai": ProviderProfile(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "OPENAI_API_KEY",
        token_parameter="max_completion_tokens",
        max_output_tokens=500,
    ),
}


def settings_for_provider(name: str) -> Settings:
    profile = PROVIDERS[name]
    key = os.getenv(profile.key_env)
    if not key:
        raise ValueError(f"set {profile.key_env} in this process before --with-model")
    return Settings(
        llm_base_url=profile.base_url,
        llm_model=profile.model,
        llm_api_key=key,
        llm_max_tokens_parameter=profile.token_parameter,
        llm_thinking_mode=profile.thinking_mode,
        llm_max_output_tokens=profile.max_output_tokens,
        llm_timeout_seconds=profile.timeout_seconds,
    )
