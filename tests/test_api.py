import pytest
from fastapi.testclient import TestClient

from aegisops.config import Profile, Settings
from apps.api.main import create_app


def test_live_does_not_require_database() -> None:
    settings = Settings(database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health/live").json() == {"status": "ok"}
        assert client.get("/api/v1/health/ready").status_code == 503


@pytest.mark.parametrize("profile", [Profile.TEST, Profile.STAGING, Profile.PRODUCTION])
def test_non_dev_api_runtime_is_rejected(profile: Profile) -> None:
    settings = Settings(
        profile=profile,
        database_url="postgresql+psycopg://separate:separate@127.0.0.1:1/control",
    )
    with pytest.raises(RuntimeError, match="production identity and tenant support"):
        create_app(settings)
