"""Responses that are technically successful and semantically garbage.

A 2xx is not proof the body came from HexRead. Captive portals, corporate proxies and CDN error
pages all answer 200 with HTML, and a job handle is just JSON: whatever it says about where to go
next, this client has to decide whether to believe it. Nothing here may escape as a bare
JSONDecodeError or AttributeError, because callers catch HexReadError and so does convert_many.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    API_KEY,
    BASE_URL,
    JOB_ID,
    ORIGIN,
    convert_payload,
    event_blocks,
    ok_response,
    sse,
    sse_response,
)

from hexread import HexReadError, ResultDeliveryError, ServerError
from hexread._transport import resolve_job_url

GOOD_EVENTS = sse_response(*event_blocks())

# Bodies a 2xx can carry that are not a HexRead payload.
BAD_BODIES = {
    "html from a proxy": httpx.Response(200, text="<html><body>403 Forbidden</body></html>"),
    "empty body": httpx.Response(200, content=b""),
    "whitespace only": httpx.Response(200, text="   \n  "),
    "a json array": httpx.Response(200, json=[1, 2, 3]),
    "a json string": httpx.Response(200, json="ok"),
    "a json number": httpx.Response(200, json=7),
    "json null": httpx.Response(200, json=None),
    "truncated json": httpx.Response(200, text='{"markdown": "hal'),
    "not utf-8": httpx.Response(200, content=b"\xff\xfe\x00\x00garbage"),
    "a bare 204": httpx.Response(204, content=b""),
    "a 202 with an html body": httpx.Response(202, text="<html>gateway</html>"),
    "a 202 that is a json array": httpx.Response(202, json=[]),
}


@pytest.mark.parametrize("response", BAD_BODIES.values(), ids=list(BAD_BODIES))
def test_sync_convert_turns_an_unreadable_success_into_a_hexread_error(
    make_api, make_client, response
):
    with pytest.raises(HexReadError) as caught:
        make_client(make_api(convert=response)).convert(b"%PDF-1.7")

    assert isinstance(caught.value, ServerError)
    assert caught.value.code == "invalid_response"
    assert len(str(caught.value)) < 400, "the body must be summarised, not pasted"


@pytest.mark.parametrize("response", BAD_BODIES.values(), ids=list(BAD_BODIES))
async def test_async_convert_turns_an_unreadable_success_into_a_hexread_error(
    make_api, make_async_client, response
):
    async with make_async_client(make_api(convert=response)) as client:
        with pytest.raises(ServerError):
            await client.convert(b"%PDF-1.7")


@pytest.mark.parametrize("response", BAD_BODIES.values(), ids=list(BAD_BODIES))
def test_usage_survives_an_unreadable_success(make_api, make_client, response):
    with pytest.raises(HexReadError):
        make_client(make_api(usage=response)).usage()


def test_a_batch_keeps_going_when_one_response_is_unreadable(make_api, make_client):
    """The whole point of returning HexReadError: convert_many catches nothing else."""
    api = make_api(
        convert=[
            ok_response(),
            httpx.Response(200, text="<html>proxy</html>"),
            ok_response(),
        ]
    )
    results = make_client(api).convert_many([b"a", b"b", b"c"], max_workers=1)

    assert not isinstance(results[0], Exception)
    assert isinstance(results[1], ServerError)
    assert not isinstance(results[2], Exception)


# -- job handles that are missing pieces --------------------------------------------------------


def test_a_job_handle_with_no_links_falls_back_to_the_canonical_paths(make_api, make_client):
    api = make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID, "status": "queued"}),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")

    assert str(api.only("events").url) == f"{BASE_URL}/jobs/{JOB_ID}/events"
    assert api.only("result").url.path == f"/v1/jobs/{JOB_ID}/result"


def test_a_job_handle_with_links_that_are_not_an_object_is_ignored(make_api, make_client):
    api = make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID, "links": "go away"}),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")
    assert str(api.only("events").url) == f"{BASE_URL}/jobs/{JOB_ID}/events"


def test_a_job_handle_with_no_job_id_still_reaches_a_result(make_api, make_client):
    api = make_api(
        convert=httpx.Response(
            202, json={"status": "queued", "links": {"events": f"/v1/jobs/{JOB_ID}/events"}}
        ),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    result = make_client(api).convert(b"%PDF-1.7")
    assert result.markdown.startswith("# Quarterly report")
    assert result.job_id == ""


def test_a_relative_link_without_a_leading_slash_is_still_joined_to_the_origin(
    make_api, make_client
):
    api = make_api(
        convert=httpx.Response(
            202, json={"job_id": JOB_ID, "links": {"events": f"v1/jobs/{JOB_ID}/events"}}
        ),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")
    assert str(api.only("events").url) == f"{ORIGIN}/v1/jobs/{JOB_ID}/events"


# -- links that point somewhere else ------------------------------------------------------------

FOREIGN_LINKS = [
    "https://evil.example.com/v1/jobs/x/events",
    "http://evil.example.com/v1/jobs/x/events",
    "//evil.example.com/v1/jobs/x/events",
    "https://api.hexread.com.evil.example.com/steal",
    "http://api.hexread.com/v1/jobs/x/events",  # same host, downgraded scheme
    "https://127.0.0.1:9/v1/jobs/x/events",
    "https://user:pass@evil.example.com/x",
]


@pytest.mark.parametrize("link", FOREIGN_LINKS)
def test_an_events_link_to_another_origin_is_not_followed(make_api, make_client, link):
    """The events request carries the API key. A response body must not be able to redirect it."""
    api = make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID, "links": {"events": link}}),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")

    events = api.only("events")
    assert events.url.host == "api.hexread.com"
    assert str(events.url) == f"{BASE_URL}/jobs/{JOB_ID}/events"
    assert events.headers["authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize("link", FOREIGN_LINKS)
def test_a_result_link_to_another_origin_is_not_followed(make_api, make_client, link):
    """The result link carries the one-time token, which is worth exactly one paid conversion."""
    completed = sse(
        "completed",
        {"event": "completed", "pages": "3", "token": "tok_read_once", "result": link},
        event_id="4",
    )
    api = make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID}),
        events=sse_response(completed),
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")

    assert api.only("result").url.host == "api.hexread.com"


@pytest.mark.parametrize("link", FOREIGN_LINKS)
async def test_the_async_client_refuses_the_same_links(make_api, make_async_client, link):
    api = make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID, "links": {"events": link}}),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    async with make_async_client(api) as client:
        await client.convert(b"%PDF-1.7")
    assert api.only("events").url.host == "api.hexread.com"


def test_a_link_back_to_our_own_origin_is_honoured(make_api, make_client):
    api = make_api(
        convert=httpx.Response(
            202,
            json={"job_id": JOB_ID, "links": {"events": f"{ORIGIN}/v1/jobs/other/events"}},
        ),
        events=GOOD_EVENTS,
        result=ok_response(),
    )
    make_client(api).convert(b"%PDF-1.7")
    assert str(api.only("events").url) == f"{ORIGIN}/v1/jobs/other/events"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("", f"{BASE_URL}/jobs/j1/events"),
        ("/v1/jobs/j1/events", f"{ORIGIN}/v1/jobs/j1/events"),
        ("v1/jobs/j1/events", f"{ORIGIN}/v1/jobs/j1/events"),
        (f"{ORIGIN}/v1/jobs/j1/events", f"{ORIGIN}/v1/jobs/j1/events"),
        ("https://elsewhere.test/x", f"{BASE_URL}/jobs/j1/events"),
        ("//elsewhere.test/x", f"{BASE_URL}/jobs/j1/events"),
        ("HTTPS://API.HEXREAD.COM/v1/jobs/j1/events", "HTTPS://API.HEXREAD.COM/v1/jobs/j1/events"),
    ],
)
def test_resolve_job_url_directly(link, expected):
    assert resolve_job_url(ORIGIN, BASE_URL, "j1", link, "/events") == expected


# -- the read-once result -------------------------------------------------------------------------


def job_api(make_api, result):
    return make_api(
        convert=httpx.Response(202, json={"job_id": JOB_ID}), events=GOOD_EVENTS, result=result
    )


@pytest.mark.parametrize(
    "result",
    [
        httpx.Response(200, content=b""),
        httpx.Response(200, text="<html>gateway timeout</html>"),
        httpx.Response(200, json=[{"markdown": "page"}]),
        httpx.Response(200, json="just a string"),
    ],
    ids=["empty", "html", "array", "string"],
)
def test_a_result_that_will_not_decode_keeps_the_bytes(make_api, make_client, result):
    """The body is served once. Whatever arrived has to survive the failure to parse it."""
    with pytest.raises(ResultDeliveryError) as caught:
        make_client(job_api(make_api, result)).convert(b"%PDF-1.7")

    assert caught.value.job_id == JOB_ID
    assert caught.value.markdown == result.text


def test_a_result_with_no_pages_array_is_still_a_result(make_api, make_client):
    body = {"markdown": "# All of it in one string", "meta": {"pages": 4}}
    result = make_client(job_api(make_api, httpx.Response(200, json=body))).convert(b"%PDF-1.7")

    assert result.pages == []
    assert result.markdown == "# All of it in one string"
    assert result.page_count == 4


def test_a_result_with_no_meta_is_still_a_result(make_api, make_client):
    body = {"markdown": "# No metadata", "pages": [{"index": 0, "markdown": "# No metadata"}]}
    result = make_client(job_api(make_api, httpx.Response(200, json=body))).convert(b"%PDF-1.7")

    assert result.page_count == 0
    assert result.meta.model == ""
    assert len(result.pages) == 1


def test_pages_that_are_not_objects_are_rejected_with_the_body_attached(make_api, make_client):
    body = {"markdown": "# Ok", "pages": ["not an object"], "meta": {}}
    with pytest.raises(ResultDeliveryError) as caught:
        make_client(job_api(make_api, httpx.Response(200, json=body))).convert(b"%PDF-1.7")
    assert "# Ok" in caught.value.markdown


def test_a_result_with_absurd_page_indexes_is_taken_at_face_value(make_api, make_client):
    body = convert_payload("# Odd", pages=[{"index": -5, "markdown": "a"}], meta={"pages": 1})
    result = make_client(job_api(make_api, httpx.Response(200, json=body))).convert(b"%PDF-1.7")
    assert result.pages[0].index == -5
    assert result.pages[0].number == -4


def test_a_quota_header_that_is_not_a_number_is_dropped(make_api, make_client):
    api = make_api(
        convert=httpx.Response(
            200,
            json=convert_payload(),
            headers={"X-HexRead-Quota-Pages-Remaining": "loads"},
        )
    )
    assert make_client(api).convert(b"%PDF-1.7").pages_remaining is None
