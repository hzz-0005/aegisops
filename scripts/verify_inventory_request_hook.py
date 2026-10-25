"""Run inside the Lab image: regression for HTTP Request versus business request body."""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request

os.environ["LAB_DATABASE_URL"] = "postgresql://unused"
os.environ["LAB_INVENTORY_URL"] = "http://inventory-service:8000"
os.environ["LAB_BAD_DEPLOYMENT"] = "false"
with patch("psycopg_pool.ConnectionPool", return_value=SimpleNamespace(close=lambda: None)):
    from lab.services import order


@contextmanager
def database():
    yield object()


def main() -> None:
    original_client = httpx.Client
    for failure in ("timeout", "transport", "http_5xx"):
        def respond(request):
            if failure == "timeout":
                raise httpx.ReadTimeout("sample", request=request)
            if failure == "transport":
                raise httpx.ConnectError("sample", request=request)
            return httpx.Response(503)

        request = Request({"type": "http", "method": "POST", "path": "/orders", "headers": []})
        body = order.CreateOrder(idempotency_key=uuid4(), sku="sku-demo", quantity=1)
        captured = []
        def mark(actual_request, **fact):
            assert actual_request is request and isinstance(actual_request, Request)
            captured.append(fact)

        with patch.object(order, "order_database", database), \
             patch.object(order, "find_order", return_value=None), \
             patch.object(order, "mark_request_failure", mark), \
             patch.object(order.httpx, "Client", lambda **_: original_client(
                 transport=httpx.MockTransport(respond),
             )):
            try:
                order.create_order(body, request)
            except HTTPException as exc:
                assert exc.status_code == 502
            else:
                raise AssertionError("failure did not reject checkout")
        assert captured == [{"service": "order-service", "failure_code": f"dependency_{failure}",
                             "resource": "inventory-service"}]
    print("inventory_request_hook=3/3")


if __name__ == "__main__":
    main()
