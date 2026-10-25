"""Provider presets must preserve the same bounded read-only model contract."""

from uuid import uuid4

import pytest

from aegisops.application.investigation.model_review import ModelCommentary
from aegisops.application.model_gateway import ModelGateway
from scripts.model_provider_profiles import PROVIDERS, settings_for_provider


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_provider_profile_uses_validated_read_only_request(monkeypatch, name: str) -> None:
    profile = PROVIDERS[name]
    monkeypatch.setenv(profile.key_env, "test-only-key")
    evidence_id = uuid4()
    seen = {}

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"summary":"supported","cited_evidence_ids":'
                                f'["{evidence_id}"],"uncertainties":[]}}'
                            )
                        },
                    }
                ]
            }

    def post(url, **kwargs):
        seen.update(url=url, body=kwargs["json"])
        return Response()

    monkeypatch.setattr("aegisops.application.model_gateway.httpx.post", post)
    result = ModelGateway(settings_for_provider(name)).generate_structured(
        ModelCommentary,
        system_instruction="Return JSON with supplied evidence IDs.",
        user_payload='{"evidence":[]}',
    )
    assert result.cited_evidence_ids == [evidence_id]
    assert seen["url"] == f"{profile.base_url}/chat/completions"
    assert seen["body"]["model"] == profile.model
    assert seen["body"][profile.token_parameter] == profile.max_output_tokens
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "tools" not in seen["body"]
    assert ("thinking" in seen["body"]) == (profile.thinking_mode == "disabled")


def test_provider_profile_requires_its_own_key(monkeypatch) -> None:
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MIMO_API_KEY"):
        settings_for_provider("mimo")
