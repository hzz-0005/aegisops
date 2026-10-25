from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


@lru_cache(maxsize=4)
def engine_for(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def session_for(database_url: str) -> Session:
    return Session(engine_for(database_url))
