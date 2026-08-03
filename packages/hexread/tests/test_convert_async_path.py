"""The job path of the sync client: 202, the event stream, then the read-once result.

Everything here drives `HexRead.convert`. The client is expected to hide the whole handshake, so
these tests assert on what a caller can see: the markdown, the progress updates, and the requests
that went out.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import (
    JOB_ID,
    KEEPALIVE,
    convert_payload,
    error_response,
    event_blocks,
    job_response,
    ok_response,
    route_of,
    sse,
    sse_response,
)

from hexread import CapacityError, GoneError, ResultDeliveryError, ValidationError

RESULT_MARKDOWN = "# Delivered by the result endpoint\n"
RESULT_META = {
    "pages": 3,
    "sha256": "5c3b9a77",
    "timing_ms": 40321,
    "backend": "vlm",
    "model": "granite-docling",
    "route_reason": "auto: scanned pdf",
}


def result_body(markdown: str = RESULT_MARKDOWN) -> dict[str, Any]:
    return convert_payload(
        markdown,
        pages=[{"index": i, "markdown": f"page {i + 1}"} for i in range(3)],
        meta=RESULT_META,
    )


def test_job_path_runs_end_to_end(make_api, make_client, pdf, sleeps):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=ok_response(result_body(), pages_remaining=95),
    )
    result = make_client(api).convert(pdf, prefer="async")

    assert result.markdown == RESULT_MARKDOWN
    assert result.job_id == JOB_ID
    assert result.model == "granite-docling"
    assert result.page_count == 3
    assert [page.number for page in result.pages] == [1, 2, 3]
    assert result.pages_remaining == 95
    assert result.source == "report.pdf"
    assert [route_of(request) for request in api.requests] == ["convert", "events", "result"]
    assert sleeps == []


def test_result_request_carries_the_token_and_supplies_the_markdown(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks(token="tok_read_once")),
        result=ok_response(result_body("# only in the result body\n")),
    )
    result = make_client(api).convert(pdf)

    request = api.only("result")
    assert request.url.params["token"] == "tok_read_once"
    assert request.url.path == f"/v1/jobs/{JOB_ID}/result"
    assert result.markdown == "# only in the result body\n"


def test_completed_without_a_token_is_not_terminal(make_api, make_client, pdf, sleeps):
    """The result cannot be fetched without a token, so the client reads on for a later one."""
    api = make_api(
        convert=job_response(),
        events=[
            sse_response(
                KEEPALIVE,
                sse("phase", {"event": "phase", "phase": "processing"}, event_id="1"),
                sse("completed", {"event": "completed", "pages": 3}, event_id="2"),
                KEEPALIVE,
                sse(
                    "completed",
                    {"event": "completed", "pages": 3, "token": "tok_late"},
                    event_id="3",
                ),
            )
        ],
        result=ok_response(result_body()),
    )
    result = make_client(api).convert(pdf)

    assert len(api.sent("events")) == 1, "the client gave up on the stream instead of reading on"
    assert api.only("result").url.params["token"] == "tok_late"
    assert result.markdown == RESULT_MARKDOWN
    assert sleeps == []


def test_a_stream_that_ends_without_a_token_is_reconnected(make_api, make_client, pdf, sleeps):
    """Same rule across a dropped connection: keep waiting for the completed that carries one."""
    api = make_api(
        convert=job_response(),
        events=[
            sse_response(*event_blocks(token="")),
            sse_response(sse("completed", {"event": "completed", "token": "tok_late"})),
        ],
        result=ok_response(result_body()),
    )
    result = make_client(api).convert(pdf)

    assert [route_of(request) for request in api.requests] == [
        "convert",
        "events",
        "events",
        "result",
    ]
    assert api.only("result").url.params["token"] == "tok_late"
    assert result.markdown == RESULT_MARKDOWN
    assert sleeps == [1.0]


def test_a_reconnect_resumes_from_the_last_event_id(make_api, make_client, pdf, sleeps):
    api = make_api(
        convert=job_response(),
        events=[
            sse_response(*event_blocks(token="")),
            sse_response(sse("completed", {"event": "completed", "token": "tok_late"})),
        ],
        result=ok_response(result_body()),
    )
    make_client(api).convert(pdf)

    first, second = api.sent("events")
    assert "last-event-id" not in first.headers
    assert second.headers["last-event-id"] == "4"


def test_job_links_are_joined_against_the_origin(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=ok_response(result_body()),
    )
    make_client(api).convert(pdf)

    assert str(api.only("events").url) == f"https://api.hexread.com/v1/jobs/{JOB_ID}/events"
    assert api.only("result").url.path == f"/v1/jobs/{JOB_ID}/result"
    assert all("/v1/v1/" not in str(request.url) for request in api.requests)


def test_job_urls_fall_back_to_the_base_url_without_links(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(links=False),
        events=sse_response(*event_blocks(result=None)),
        result=ok_response(result_body()),
    )
    make_client(api).convert(pdf)

    assert str(api.only("events").url) == f"https://api.hexread.com/v1/jobs/{JOB_ID}/events"
    assert api.only("result").url.path == f"/v1/jobs/{JOB_ID}/result"
    assert all("/v1/v1/" not in str(request.url) for request in api.requests)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("encrypted_pdf", ValidationError),
        ("too_many_pages", ValidationError),
        ("conversion_failed", CapacityError),
        ("a_code_this_client_has_never_heard_of", CapacityError),
    ],
)
def test_an_sse_error_event_raises_the_mapped_class(make_api, make_client, pdf, code, expected):
    api = make_api(
        convert=job_response(),
        events=sse_response(
            KEEPALIVE,
            sse("phase", {"event": "phase", "phase": "processing"}),
            sse("error", {"event": "error", "code": code}),
        ),
        result=ok_response(result_body()),
    )
    with pytest.raises(expected) as caught:
        make_client(api).convert(pdf)

    assert caught.value.code == code
    assert JOB_ID in caught.value.message
    assert api.sent("result") == []


def test_progress_reports_phase_and_page_counts(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=ok_response(result_body()),
    )
    seen = []
    make_client(api).convert(pdf, on_progress=seen.append)

    assert [update.phase for update in seen] == [
        "uploading",
        "queued",
        "warming",
        "processing",
        "processing",
        "completed",
    ]
    assert (seen[3].pages_done, seen[3].pages_total) == (1, 3)
    assert seen[3].fraction == pytest.approx(1 / 3)
    assert (seen[-1].pages_done, seen[-1].pages_total) == (3, 3)
    assert all(update.source == "report.pdf" for update in seen)


def test_a_consumed_result_raises_gone_with_the_job_id(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=error_response(410, type="gone", code="result_consumed"),
    )
    with pytest.raises(GoneError) as caught:
        make_client(api).convert(pdf)

    assert f"(job {JOB_ID})" in caught.value.message
    assert "already retrieved" in caught.value.message


def test_an_undecodable_result_body_keeps_the_markdown_on_the_exception(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=httpx.Response(200, text="# markdown that is not json\n"),
    )
    with pytest.raises(ResultDeliveryError) as caught:
        make_client(api).convert(pdf)

    assert caught.value.markdown == "# markdown that is not json\n"
    assert caught.value.job_id == JOB_ID
