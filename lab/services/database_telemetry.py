"""Bounded read operations; emit server error facts without SQL or parameter values."""

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import HTTPException, Request
from opentelemetry import trace
from opentelemetry.trace.status import Status, StatusCode

from lab.services.common import database, mark_request_failure


@contextmanager
def observed_database_read(request: Request, *, service: str,
                           operation: str) -> Iterator[psycopg.Connection]:
    lock_ms = int(os.getenv("LAB_DB_READ_LOCK_TIMEOUT_MS", "1000"))
    statement_ms = int(os.getenv("LAB_DB_READ_STATEMENT_TIMEOUT_MS", "2000"))
    if not 0 < lock_ms < statement_ms <= 10000:
        raise ValueError("read timeouts must satisfy 0 < lock < statement <= 10000ms")
    started = time.monotonic()
    with trace.get_tracer("aegisops.lab.database").start_as_current_span(
        "database.read", record_exception=False, set_status_on_exception=False,
    ) as span:
        span.set_attributes({"db.system.name": "postgresql", "db.operation.name": "SELECT",
            "aegisops.db.operation": operation, "aegisops.db.lock_timeout_ms": lock_ms,
            "aegisops.db.statement_timeout_ms": statement_ms})
        executing = False
        try:
            with database() as connection:
                connection.execute("SELECT set_config('lock_timeout', %s, true)",
                                   (f"{lock_ms}ms",))
                connection.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (f"{statement_ms}ms",))
                executing = True
                yield connection
        except psycopg.Error as exc:
            sqlstate = exc.sqlstate or "UNKNOWN"
            if not re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                sqlstate = "UNKNOWN"
            # These reads never use NOWAIT; 55P03 after timeout setup is a lock wait timeout.
            code = ("database_lock_timeout" if executing and sqlstate == "55P03"
                    else "database_query_cancelled" if sqlstate == "57014"
                    else "database_error")
            span.set_attribute("db.response.status_code", sqlstate)
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            mark_request_failure(request, service=service, failure_code=code,
                                 resource="lab-orders-postgres")
            raise HTTPException(status_code=503, detail="database read unavailable") from exc
        finally:
            span.set_attribute("aegisops.db.duration_ms", (time.monotonic() - started) * 1000)
