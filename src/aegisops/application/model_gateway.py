"""Single bounded provider boundary for optional structured model calls."""

import httpx
from pydantic import BaseModel

from aegisops.config import Settings


class ModelGateway:
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_base_url or not settings.llm_model or settings.llm_api_key is None:
            raise ValueError("model endpoint, model and API key must be configured")
        api_key = settings.llm_api_key.get_secret_value()
        if not api_key:
            raise ValueError("model API key must be configured")
        self.settings = settings
        self.api_key = api_key

    def generate_structured[T: BaseModel](
        self, output_type: type[T], *, system_instruction: str, user_payload: str
    ) -> T:
        result, _ = self.generate_with_usage(
            output_type, system_instruction=system_instruction, user_payload=user_payload
        )
        return result

    def generate_with_usage[T: BaseModel](
        self, output_type: type[T], *, system_instruction: str, user_payload: str,
        timeout_seconds: float | None = None,
    ) -> tuple[T, dict[str, int]]:
        if len(user_payload) > 8000:
            raise ValueError("model input budget exceeded")
        settings = self.settings
        assert settings.llm_base_url is not None and settings.llm_model is not None
        request_body: dict = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_payload},
            ],
            settings.llm_max_tokens_parameter: settings.llm_max_output_tokens,
        }
        if settings.llm_response_format == "json_object":
            request_body["response_format"] = {"type": "json_object"}
        if settings.llm_thinking_mode == "disabled":
            request_body["thinking"] = {"type": "disabled"}
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=request_body,
            timeout=min(settings.llm_timeout_seconds, timeout_seconds)
            if timeout_seconds is not None else settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        try:
            choice = data["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError("model response did not finish normally")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("model response has no text content")
            usage = data.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            safe_usage = {key: value for key, value in usage.items()
                          if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                          and isinstance(value, int) and value >= 0}
            return output_type.model_validate_json(content), safe_usage
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("model response has an invalid envelope") from exc
