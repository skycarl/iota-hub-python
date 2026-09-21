"""Test fixtures: a scriptable in-process API built on ``httpx.MockTransport``.

No network, no LocalStack, no ``respx``. A test registers the responses it
expects by ``(method, path)`` and afterwards reads back every request that was
sent, so header, body and query assertions are ordinary assertions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from iota_hub import Client

BASE_URL = "https://api.example.test"
API_KEY = "iotahub_test_x"

SPEC_PATH = Path(__file__).resolve().parent.parent / "spec" / "openapi.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class MockAPI:
    """Responses in, requests out.

    ``add`` takes one response or several: a scripted list is consumed in
    order, and the last one repeats, so a retry test registers ``429`` then
    ``200`` and a "never retried" test registers a single response and asserts
    how many requests arrived. An ``Exception`` in the script is raised instead
    of answered, which is how a transport failure is simulated.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], list[Any]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path: str, *responses: Any) -> None:
        self._routes.setdefault((method.upper(), path), []).extend(responses)

    def json(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Register one JSON response."""
        self.add(method, path, httpx.Response(status, json=body, headers=headers))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        queue = self._routes.get(key)
        if not queue:
            raise AssertionError(f"unexpected request: {key[0]} {key[1]}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def body_of(request: httpx.Request) -> Any:
    """The request's JSON body."""
    return json.loads(request.content)


@pytest.fixture
def mock_api() -> MockAPI:
    return MockAPI()


@pytest.fixture
def sleeps() -> list[float]:
    """Every delay the transport would have slept for."""
    return []


@pytest.fixture
def client(mock_api: MockAPI, sleeps: list[float]) -> Iterator[Client]:
    with Client(
        API_KEY,
        BASE_URL,
        transport=mock_api.transport,
        sleep=sleeps.append,
    ) as client:
        yield client
