"""Fixtures for the offline test suite.

Every test runs against an in-process API served by httpx.MockTransport, so no socket is opened
and no credential on the machine is read. `make_api` builds the fake, `make_client` wires a client
to it, and the builders below shape the bodies each route returns.

A route takes one response (served for every call), a callable, or a list consumed in order. A list
that runs out fails the test: it means the client sent a request nobody expected.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from hexread import AsyncHexRead, HexRead, _auth

API_KEY = "hr_test_2f8c1bd4"
BASE_URL = "https://api.hexread.com/v1"
ORIGIN = "https://api.hexread.com"
JOB_ID = "job_2f8c1bd4"
QUOTA_HEADER = "X-HexRead-Quota-Pages-Remaining"

ROUTES = ("convert", "events", "result", "usage", "cancel")

Handler = Callable[[httpx.Request], httpx.Response]
Route = Any

MARKDOWN = "# Quarterly report\n\nRevenue rose by 12 percent.\n"
PAGES: list[dict[str, Any]] = [
    {"index": 0, "markdown": "# Quarterly report"},
    {"index": 1, "markdown": "Revenue rose by 12 percent."},
]
META: dict[str, Any] = {
    "pages": 2,
    "sha256": "9f2c4a1d",
    "timing_ms": 8421,
    "backend": "vlm",
    "model": "mineru-2.5",
    "route_reason": "auto: text-light pdf",
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: talks to the real HexRead API; runs only when HEXREAD_API_KEY is set"
    )
    config.addinivalue_line(
        "markers", "slow: takes long enough to be worth deselecting with -m 'not slow'"
    )


# -- payload and response builders -------------------------------------------------------------


def convert_payload(
    markdown: str = MARKDOWN,
    *,
    pages: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ConvertResult body."""
    return {
        "markdown": markdown,
        "pages": PAGES if pages is None else pages,
        "meta": META if meta is None else meta,
    }


def ok_response(
    payload: dict[str, Any] | None = None,
    *,
    status: int = 200,
    pages_remaining: int | None = 498,
) -> httpx.Response:
    """A converted document, inline or from the read-once result endpoint."""
    headers = {} if pages_remaining is None else {QUOTA_HEADER: str(pages_remaining)}
    return httpx.Response(
        status, json=convert_payload() if payload is None else payload, headers=headers
    )


def job_response(
    *, job_id: str = JOB_ID, status: str = "queued", links: bool = True
) -> httpx.Response:
    """The 202 handle the server returns when a conversion is too slow to answer inline."""
    body: dict[str, Any] = {"job_id": job_id, "status": status}
    if links:
        body["links"] = {
            "events": f"/v1/jobs/{job_id}/events",
            "result": f"/v1/jobs/{job_id}/result",
            "cancel": f"/v1/jobs/{job_id}",
        }
    return httpx.Response(202, json=body)


def error_response(
    status: int,
    *,
    type: str = "",
    code: str = "",
    message: str = "",
    request_id: str = "req_test",
    doc_url: str = "https://hexread.com/api",
    retry_after: float | str | None = None,
) -> httpx.Response:
    """A HexRead error envelope."""
    envelope = {
        "type": type,
        "code": code,
        "message": message,
        "doc_url": doc_url,
        "request_id": request_id,
    }
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return httpx.Response(status, json=envelope, headers=headers)


def usage_response(**overrides: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "tier": "pro",
        "pages": {"used": 102, "remaining": 2398, "allowance": 2500, "reserved": 3},
        "concurrency": {"in_use": 1, "limit": 4},
        "api_access": True,
        "available_models": ["auto", "mineru-2.5", "granite-docling"],
        "period": "monthly",
        "resets_at": "2026-08-14T00:00:00Z",
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


KEEPALIVE = ": keepalive\n\n"


def sse(event: str, data: Mapping[str, Any] | None = None, *, event_id: str = "") -> str:
    """One SSE block, ending with the blank line that dispatches it.

    Values are stringified because that is what the server sends: every field arrives as a string.
    """
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    if data is not None:
        lines.append("data: " + json.dumps({str(k): str(v) for k, v in data.items()}))
    return "\n".join(lines) + "\n\n"


def sse_response(*blocks: str) -> httpx.Response:
    return httpx.Response(200, text="".join(blocks), headers={"content-type": "text/event-stream"})


def event_blocks(
    *, token: str = "tok_read_once", result: str | None = f"/v1/jobs/{JOB_ID}/result"
) -> tuple[str, ...]:
    """The stream of a healthy three page conversion, keepalive included."""
    completed: dict[str, Any] = {"event": "completed", "pages": 3}
    if token:
        completed["token"] = token
    if result:
        completed["result"] = result
    return (
        KEEPALIVE,
        sse("phase", {"event": "phase", "phase": "warming"}, event_id="1"),
        sse("page", {"event": "page", "page": 1, "total": 3}, event_id="2"),
        sse("page", {"event": "page", "page": 3, "total": 3}, event_id="3"),
        sse("completed", completed, event_id="4"),
    )


# -- the fake API ------------------------------------------------------------------------------


def route_of(request: httpx.Request) -> str:
    """Which endpoint a request is for."""
    path = request.url.path
    for suffix in ("convert", "events", "result", "usage"):
        if path.endswith(f"/{suffix}"):
            return suffix
    if "/jobs/" in path and request.method == "DELETE":
        return "cancel"
    raise AssertionError(f"the client called an endpoint the fake does not serve: {request.url}")


def _as_handlers(route: Route) -> list[Handler]:
    items = list(route) if isinstance(route, (list, tuple)) else [route]
    return [item if callable(item) else _constant(item) for item in items]


def _constant(response: httpx.Response) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return handler


class MockAPI:
    """A stand-in for the HexRead API that records every request it served."""

    def __init__(self, **routes: Route) -> None:
        unknown = sorted(set(routes) - set(ROUTES))
        if unknown:
            raise ValueError(f"unknown routes {unknown}, expected any of {list(ROUTES)}")
        self.requests: list[httpx.Request] = []
        self._queues = {name: _as_handlers(route) for name, route in routes.items()}
        self._repeat = {
            name: not isinstance(route, (list, tuple)) for name, route in routes.items()
        }
        self.transport = httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = route_of(request)
        queue = self._queues.get(route)
        if queue is None:
            raise AssertionError(f"the fake serves no {route!r} route, but the client called it")
        if self._repeat[route]:
            return queue[0](request)
        if not queue:
            raise AssertionError(
                f"the client sent more {route!r} requests than the fake was given responses for"
            )
        return queue.pop(0)(request)

    def sent(self, route: str) -> list[httpx.Request]:
        """Every request the client sent to one endpoint, in order."""
        return [request for request in self.requests if route_of(request) == route]

    def only(self, route: str) -> httpx.Request:
        """The single request sent to one endpoint."""
        sent = self.sent(route)
        assert len(sent) == 1, f"expected exactly one {route} request, saw {len(sent)}"
        return sent[0]


# -- multipart inspection ----------------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """One part of a multipart body."""

    name: str
    value: bytes
    filename: str | None = None


def parts_of(request: httpx.Request) -> list[Part]:
    content_type = request.headers["content-type"]
    boundary = content_type.partition("boundary=")[2].strip().strip('"')
    assert boundary, f"not a multipart request: {content_type}"
    parts: list[Part] = []
    for chunk in request.content.split(b"--" + boundary.encode()):
        # A part is "\r\n<headers>\r\n\r\n<bytes>\r\n"; the epilogue after the last boundary is
        # "--\r\n". Only the framing newlines come off, so a payload keeps its own trailing one.
        block = chunk.lstrip(b"\r\n")
        if not block or block.startswith(b"--"):
            continue
        head, _, value = block.partition(b"\r\n\r\n")
        value = value[:-2] if value.endswith(b"\r\n") else value
        disposition = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', disposition)
        filename = re.search(r'filename="([^"]*)"', disposition)
        parts.append(
            Part(
                name=name.group(1) if name else "",
                value=value,
                filename=filename.group(1) if filename else None,
            )
        )
    return parts


def form_fields(request: httpx.Request) -> dict[str, str]:
    """The non-file fields of a multipart body."""
    return {
        part.name: part.value.decode("utf-8") for part in parts_of(request) if part.filename is None
    }


def file_part(request: httpx.Request) -> Part:
    """The uploaded file part."""
    for part in parts_of(request):
        if part.filename is not None:
            return part
    raise AssertionError("the request carried no file part")


# -- fixtures ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def offline_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hide the machine's own credentials and base URL from the offline tests.

    Without this a developer with HEXREAD_API_KEY exported, or a CLI login on disk, would get
    different results from CI. Live tests opt out: the real environment is the point there.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in (_auth.API_KEY_ENV, _auth.BASE_URL_ENV, _auth.BASE_URL_ENV_LEGACY):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_auth, "credential_path", lambda: tmp_path / "no-credential")


@pytest.fixture
def make_api() -> Callable[..., MockAPI]:
    """Build a fake API. One keyword per route: convert, events, result, usage, cancel."""

    def factory(**routes: Route) -> MockAPI:
        return MockAPI(**routes)

    return factory


@pytest.fixture
def make_client() -> Iterator[Callable[..., HexRead]]:
    """Build a client wired to a fake API. Every client it hands out is closed at teardown."""
    clients: list[HexRead] = []

    def factory(api: MockAPI, **kwargs: Any) -> HexRead:
        kwargs.setdefault("api_key", API_KEY)
        kwargs.setdefault("base_url", BASE_URL)
        client = HexRead(transport=api.transport, **kwargs)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def make_async_client() -> Callable[..., AsyncHexRead]:
    """Build an async client wired to a fake API. Drive it with `async with`, which closes it."""

    def factory(api: MockAPI, **kwargs: Any) -> AsyncHexRead:
        kwargs.setdefault("api_key", API_KEY)
        kwargs.setdefault("base_url", BASE_URL)
        return AsyncHexRead(transport=api.transport, **kwargs)

    return factory


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sleep instead of taking it, so retry and reconnect paths run instantly."""
    recorded: list[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def async_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The same for the async client, which waits on asyncio.sleep."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
        recorded.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    """A small file on disk that stands in for a real document."""
    path = tmp_path / "report.pdf"
    path.write_bytes(PDF_BYTES)
    return path
