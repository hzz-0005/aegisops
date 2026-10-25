"""Unavailable responses must not become confirmed data integrity violations."""

import asyncio

import httpx
import pytest

from lab.services.health_probe import probe, probe_assertion

SPEC = {"name": "currency", "service": "inventory-service", "url": "http://inventory/item",
        "field": "currency", "equals": "CNY"}


@pytest.mark.parametrize("status,body,expected", [
    (200, {"currency": "CNY"}, (1, 1)),
    (200, {"currency": "USD"}, (0, 1)),
    (200, {}, (0, 1)),
    (503, {"currency": "USD"}, (0, 0)),
    (200, [], (0, 0)),
])
def test_assertion_only_evaluates_usable_successful_response(status, body, expected):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json=body)
        )) as client:
            result = await probe_assertion(client, SPEC)
            assert result[2:] == expected
    asyncio.run(run())


@pytest.mark.parametrize("failure,category", [
    (httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport"),
])
def test_transport_failure_is_unobserved_contract_and_classified_probe(failure, category):
    def handle(request):
        raise failure("test failure", request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert (await probe_assertion(client, SPEC))[2:] == (0, 0)
            result = await probe(client, "inventory-service", SPEC["url"])
            assert result.success == 0 and result.failure == category and result.duration >= 0
    asyncio.run(run())


def test_invalid_json_is_unobserved_and_http_failure_has_distinct_category():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="invalid JSON")
        )) as client:
            assert (await probe_assertion(client, SPEC))[2:] == (0, 0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(503)
        )) as client:
            result = await probe(client, "inventory-service", SPEC["url"])
            assert result.success == 0 and result.failure == "http_status"
    asyncio.run(run())
