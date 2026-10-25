"""A Lab service may assert only its own request failure events."""

import pytest
from pydantic import ValidationError

from aegisops.application.request_events import (
    RequestEventIdentityUnavailable,
    RequestEventSourceUnauthorized,
    authorize_request_event_source,
)
from aegisops.config import Settings

ORDER_TOKEN = "order-test-token"
GATEWAY_TOKEN = "gateway-test-token"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        request_event_service_tokens={
            "order-service": ORDER_TOKEN,
            "lab-gateway": GATEWAY_TOKEN,
        },
    )


@pytest.mark.parametrize(
    ("service", "token"),
    [("order-service", ORDER_TOKEN), ("lab-gateway", GATEWAY_TOKEN)],
)
def test_service_token_accepts_only_matching_service(
    settings: Settings, service: str, token: str
) -> None:
    authorize_request_event_source(settings, f"Bearer {token}", service)


@pytest.mark.parametrize(
    ("service", "token"),
    [
        ("order-service", GATEWAY_TOKEN),
        ("lab-gateway", ORDER_TOKEN),
        ("payment-service", ORDER_TOKEN),
        ("order-service", "aegisops_request_event_dev_only"),
        ("order-service", ""),
    ],
)
def test_other_service_or_old_shared_token_cannot_write(
    settings: Settings, service: str, token: str
) -> None:
    with pytest.raises(RequestEventSourceUnauthorized):
        authorize_request_event_source(settings, f"Bearer {token}", service)


def test_missing_service_configuration_is_distinct_from_bad_identity() -> None:
    settings = Settings(_env_file=None, request_event_service_tokens={"order-service": ""})
    with pytest.raises(RequestEventIdentityUnavailable):
        authorize_request_event_source(settings, f"Bearer {ORDER_TOKEN}", "order-service")


def test_two_services_cannot_share_a_token() -> None:
    with pytest.raises(ValidationError, match="tokens must be unique"):
        Settings(
            _env_file=None,
            request_event_service_tokens={"order-service": ORDER_TOKEN, "lab-gateway": ORDER_TOKEN},
        )
