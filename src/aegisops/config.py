from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGISOPS_", env_file=".env", extra="ignore")

    profile: Profile = Profile.DEV
    database_url: str = Field(
        default="postgresql+psycopg://aegisops:aegisops_dev_only@127.0.0.1:5432/aegisops",
        min_length=1,
    )
    otel_enabled: bool = False
    dev_tenant_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    alert_webhook_token: str | None = None
    request_event_service_tokens: dict[str, str] = Field(default_factory=dict)
    operator_token: str | None = None
    approver_token: str | None = None
    rabbitmq_url: str = "amqp://aegisops:aegisops_dev_only@127.0.0.1:5672/"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "aegisops-incidents-v1"
    temporal_start_reconcile_after_hours: int = Field(default=12, ge=1)
    workflow_review_timeout_seconds: int = Field(default=86400, ge=1)
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    tempo_url: str = "http://127.0.0.1:3200"
    investigation_tool_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    investigation_agent_enabled: bool = False
    investigation_agent_seconds: float = Field(default=90, ge=5, le=90)
    investigation_agent_model_calls: int = Field(default=4, ge=1, le=4)
    investigation_agent_tool_calls: int = Field(default=8, ge=1, le=8)
    opa_url: str = "http://127.0.0.1:8181"
    remediation_kill_switch: bool = True
    lab_payment_url: str = "http://127.0.0.1:8002"
    lab_order_url: str = "http://127.0.0.1:8003"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    llm_response_format: Literal["json_object", "text"] = "json_object"
    llm_thinking_mode: Literal["default", "disabled"] = "default"
    llm_max_output_tokens: int = Field(default=500, ge=100, le=2000)
    llm_timeout_seconds: float = Field(default=20, gt=1, le=60)

    @model_validator(mode="after")
    def reject_dev_database_outside_dev(self) -> "Settings":
        tokens = list(self.request_event_service_tokens.values())
        if len(tokens) != len(set(tokens)):
            raise ValueError("request event service tokens must be unique")
        if (
            self.profile in {Profile.STAGING, Profile.PRODUCTION}
            and "aegisops_dev_only" in self.database_url
        ):
            raise ValueError("staging and production require an externally supplied database URL")
        return self


def require_dev_runtime(settings: Settings) -> None:
    """Do not start Dev identity and tenant plumbing under a non-Dev profile."""
    if settings.profile is not Profile.DEV:
        raise RuntimeError("non-dev runtime requires production identity and tenant support")
