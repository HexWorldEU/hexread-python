"""AsyncHexRead, which must behave exactly like the sync client with awaits in front.

The two clients share the pure helpers but not the I/O, and the async event stream has its own
block buffering, so the paths that could drift are all exercised here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import (
    API_KEY,
    JOB_ID,
    PDF_BYTES,
    convert_payload,
    error_response,
    event_blocks,
    file_part,
    form_fields,
    job_response,
    ok_response,
    route_of,
    sse,
    sse_response,
    usage_response,
)

from hexread import (
    CapacityError,
    PermissionError,
    QuotaError,
    ResultDeliveryError,
    ValidationError,
)

RESULT_MARKDOWN = "# Delivered by the result endpoint\n"


def result_body() -> dict[str, Any]:
    return convert_payload(
        RESULT_MARKDOWN,
        pages=[{"index": i, "markdown": f"page {i + 1}"} for i in range(3)],
        meta={
            "pages": 3,
            "sha256": "5c3b9a77",
            "timing_ms": 40321,
            "backend": "vlm",
            "model": "granite-docling",
        },
    )


async def test_inline_conversion_matches_the_sync_client(make_api, make_async_client, pdf):
    api = make_api(convert=ok_response(pages_remaining=498))
    async with make_async_client(api) as client:
        result = await client.convert(pdf)

    assert result.markdown.startswith("# Quarterly report")
    assert [page.index for page in result.pages] == [0, 1]
    assert result.model == "mineru-2.5"
    assert result.page_count == 2
    assert result.pages_remaining == 498
    assert result.source == "report.pdf"
    assert result.job_id == ""


async def test_the_request_carries_auth_the_file_and_the_fields(make_api, make_async_client, pdf):
    api = make_api(convert=ok_response())
    async with make_async_client(api) as client:
        await client.convert(pdf, model="granite-docling", lang="de")

    request = api.only("convert")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["idempotency-key"].startswith("py-")
    assert file_part(request).value == PDF_BYTES
    assert form_fields(request) == {"model": "granite-docling", "lang": "de"}


async def test_the_job_path_runs_end_to_end(make_api, make_async_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=ok_response(result_body(), pages_remaining=95),
    )
    seen = []
    async with make_async_client(api) as client:
        result = await client.convert(pdf, prefer="async", on_progress=seen.append)

    assert result.markdown == RESULT_MARKDOWN
    assert result.job_id == JOB_ID
    assert result.model == "granite-docling"
    assert result.pages_remaining == 95
    assert api.only("result").url.params["token"] == "tok_read_once"
    assert str(api.only("events").url) == f"https://api.hexread.com/v1/jobs/{JOB_ID}/events"
    assert [update.phase for update in seen] == [
        "uploading",
        "queued",
        "warming",
        "processing",
        "processing",
        "completed",
    ]
    assert (seen[3].pages_done, seen[3].pages_total) == (1, 3)


async def test_a_completed_without_a_token_is_not_terminal(make_api, make_async_client, pdf):
    """The block buffering must not stop the stream on a completed that carries no token."""
    api = make_api(
        convert=job_response(),
        events=[
            sse_response(
                sse("phase", {"event": "phase", "phase": "processing"}, event_id="1"),
                sse("completed", {"event": "completed", "pages": 3}, event_id="2"),
                sse(
                    "completed",
                    {"event": "completed", "pages": 3, "token": "tok_late"},
                    event_id="3",
                ),
            )
        ],
        result=ok_response(result_body()),
    )
    async with make_async_client(api) as client:
        result = await client.convert(pdf)

    assert len(api.sent("events")) == 1, "the client gave up on the stream instead of reading on"
    assert api.only("result").url.params["token"] == "tok_late"
    assert result.markdown == RESULT_MARKDOWN


async def test_a_stream_that_ends_without_a_token_is_reconnected(
    make_api, make_async_client, pdf, async_sleeps
):
    api = make_api(
        convert=job_response(),
        events=[
            sse_response(*event_blocks(token="")),
            sse_response(sse("completed", {"event": "completed", "token": "tok_late"})),
        ],
        result=ok_response(result_body()),
    )
    async with make_async_client(api) as client:
        result = await client.convert(pdf)

    assert [route_of(request) for request in api.requests] == [
        "convert",
        "events",
        "events",
        "result",
    ]
    assert api.sent("events")[1].headers["last-event-id"] == "4"
    assert api.only("result").url.params["token"] == "tok_late"
    assert result.markdown == RESULT_MARKDOWN
    assert async_sleeps == [1.0]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("encrypted_pdf", ValidationError), ("never_seen_this_code", CapacityError)],
)
async def test_an_sse_error_event_raises_the_mapped_class(
    make_api, make_async_client, pdf, code, expected
):
    api = make_api(
        convert=job_response(),
        events=sse_response(sse("error", {"event": "error", "code": code})),
    )
    async with make_async_client(api) as client:
        with pytest.raises(expected) as caught:
            await client.convert(pdf)

    assert caught.value.code == code
    assert api.sent("result") == []


async def test_an_undecodable_result_body_keeps_the_markdown(make_api, make_async_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=httpx.Response(200, text="# markdown that is not json\n"),
    )
    async with make_async_client(api) as client:
        with pytest.raises(ResultDeliveryError) as caught:
            await client.convert(pdf)

    assert caught.value.markdown == "# markdown that is not json\n"
    assert caught.value.job_id == JOB_ID


async def test_a_rate_limit_is_retried_with_the_same_idempotency_key(
    make_api, make_async_client, pdf, async_sleeps
):
    api = make_api(
        convert=[
            error_response(429, type="rate_limit_error", code="rate_limited", retry_after=2),
            ok_response(),
        ]
    )
    async with make_async_client(api) as client:
        result = await client.convert(pdf)

    keys = [request.headers["idempotency-key"] for request in api.sent("convert")]
    assert result.page_count == 2
    assert keys[0] == keys[1]
    assert async_sleeps == [2.0]


async def test_a_permission_error_is_raised_at_once(make_api, make_async_client, pdf, async_sleeps):
    api = make_api(
        convert=error_response(403, type="permission_error", code="api_not_available_on_tier")
    )
    async with make_async_client(api) as client:
        with pytest.raises(PermissionError):
            await client.convert(pdf)

    assert len(api.requests) == 1
    assert async_sleeps == []


async def test_usage_and_cancel(make_api, make_async_client):
    api = make_api(usage=usage_response(), cancel=httpx.Response(204))
    async with make_async_client(api) as client:
        usage = await client.usage()
        await client.cancel("job_1")

    assert usage.tier == "pro"
    assert usage.pages_remaining == 2398
    assert api.only("cancel").method == "DELETE"


async def test_convert_many_keeps_input_order_and_captures_failures(
    make_api, make_async_client, tmp_path
):
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(PDF_BYTES)
    second.write_bytes(PDF_BYTES)

    def by_name(request: httpx.Request) -> httpx.Response:
        name = file_part(request).filename
        if name == "b.pdf":
            return error_response(402, type="quota_error", code="quota_exceeded")
        return ok_response(convert_payload(markdown=f"# {name}"))

    api = make_api(convert=by_name)
    async with make_async_client(api) as client:
        results = await client.convert_many([first, second], max_workers=2)
        assert await client.convert_many([]) == []

    assert results[0].markdown == "# a.pdf"
    assert isinstance(results[1], QuotaError)


async def test_aclose_closes_the_http_client(make_api, make_async_client):
    api = make_api(convert=ok_response())
    client = make_async_client(api)
    assert (await client.convert(PDF_BYTES)).page_count == 2
    await client.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await client.convert(PDF_BYTES)
