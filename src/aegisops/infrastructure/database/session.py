from sqlalchemy import create_engine, text

from aegisops.config import Settings


def check_database(settings: Settings) -> bool:
    engine = create_engine(
        settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2}
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        engine.dispose()
