"""Extract an exact, timed database span; cancellation alone does not prove a lock."""

import base64
import binascii
import math
import re
from datetime import UTC, datetime

from aegisops.application.request_events import RequestFailureEventView, trace_fact_status


def database_span_fact(batches: list[dict], event: RequestFailureEventView) -> dict | None:
    if trace_fact_status(batches, event) != "VERIFIED_SPAN_FACT":
        return None
    for batch in batches:
        attrs = {a.get("key"): a.get("value", {}).get("stringValue")
                 for a in batch.get("resource", {}).get("attributes", [])}
        if attrs.get("service.name") != event.service:
            continue
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                try:
                    span_id = base64.b64decode(span.get("spanId", ""), validate=True).hex()
                    if span_id != event.span_id:
                        continue
                    trace_id = base64.b64decode(span.get("traceId", ""), validate=True).hex()
                    if trace_id != event.trace_id:
                        continue
                    start = datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, UTC)
                    end = datetime.fromtimestamp(int(span["endTimeUnixNano"]) / 1e9, UTC)
                    if not start <= event.occurred_at <= end or (end - start).total_seconds() > 30:
                        continue
                    values = {a["key"]: next(iter(a["value"].values()))
                              for a in span.get("attributes", [])}
                    sqlstate = values.get("db.response.status_code", "UNKNOWN")
                    operation = values.get("aegisops.db.operation", "")
                    lock = int(values["aegisops.db.lock_timeout_ms"])
                    statement = int(values["aegisops.db.statement_timeout_ms"])
                    duration = float(values["aegisops.db.duration_ms"])
                    if (values.get("db.system.name") != "postgresql"
                            or values.get("db.operation.name") != "SELECT"
                            or not isinstance(sqlstate, str)
                            or not re.fullmatch(r"[A-Z0-9]{5}|UNKNOWN", sqlstate)
                            or not isinstance(operation, str)
                            or not re.fullmatch(r"[a-z][a-z0-9_.]{1,79}", operation)
                            or not 0 < lock < statement <= 10000
                            or not math.isfinite(duration) or not 0 <= duration <= 30000
                            or span.get("status", {}).get("code") != "STATUS_CODE_ERROR"):
                        continue
                    if (event.failure_code == "database_lock_timeout" and sqlstate != "55P03"
                            or event.failure_code == "database_query_cancelled"
                            and sqlstate != "57014"):
                        continue
                    return {"operation": operation, "sqlstate": sqlstate,
                            "lock_timeout_ms": lock, "statement_timeout_ms": statement,
                            "duration_ms": round(duration, 3), "span_started_at": start.isoformat(),
                            "span_ended_at": end.isoformat()}
                except (KeyError, ValueError, TypeError, StopIteration, OverflowError,
                        binascii.Error):
                    continue
    return None
