"""Every failure shape the wire can produce, and the retry policy under a server that never yields.

Two properties are load bearing. First, no status and no body may produce anything but a
HexReadError with a readable message. Second, the retry loop has to be bounded in both attempts
and wall clock, whatever the server puts in Retry-After, because a client that sleeps on a
server-controlled number is a client the server can hang.
"""

from __future__ import annotations

import time

import httpx
import pytest
from conftest import error_response, ok_response

from hexread import (
    AuthenticationError,
    CapacityError,
    ConflictError,
    GoneError,
    HexReadError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionError,
    QuotaError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from hexread._errors import error_from_job_code, error_from_response
from hexread._transport import MAX_BACKOFF, MAX_RETRIES, retry_delay

STATUSES = list(range(400, 600))


@pytest.mark.parametrize("status", STATUSES)
def test_every_status_produces_a_usable_exception(status):
    exc = error_from_response(status, b'{"code": "", "message": ""}', {})

    assert isinstance(exc, HexReadError)
    assert exc.status == status
    assert str(exc), "an exception with no message is useless in a traceback"
    assert repr(exc)


@pytest.mark.parametrize("status", STATUSES)
def test_the_client_raises_rather_than_crashes_on_any_status(make_api, make_client, status):
    api = make_api(convert=httpx.Response(status, json={"type": "", "code": ""}))
    with pytest.raises(HexReadError):
        make_client(api, max_retries=0).convert(b"%PDF-1.7")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ValidationError),
        (401, AuthenticationError),
        (402, QuotaError),
        (403, PermissionError),
        (404, NotFoundError),
        (407, AuthenticationError),
        (409, ConflictError),
        (410, GoneError),
        (413, PayloadTooLargeError),
        (418, ValidationError),
        (429, RateLimitError),
        (500, ServerError),
        (502, CapacityError),
        (503, CapacityError),
        (504, CapacityError),
        (599, ServerError),
    ],
)
def test_a_status_with_no_envelope_still_lands_in_the_right_class(status, expected):
    assert isinstance(error_from_response(status, b"", None), expected)


BAD_ENVELOPES = {
    "all fields null": b'{"type": null, "code": null, "message": null, "request_id": null}',
    "wrong types": b'{"type": 7, "code": [], "message": {"a": 1}, "request_id": 3.5}',
    "unexpected extras": b'{"type": "quota_error", "code": "quota_exceeded", "hint": {"x": [1]},'
    b' "retry_in": 4, "nested": {"deep": {"deeper": true}}}',
    "empty object": b"{}",
    "a json array": b"[1, 2, 3]",
    "a json string": b'"nope"',
    "not json": b"<html>gateway</html>",
    "empty": b"",
    "whitespace": b"   ",
    "invalid utf-8": b"\xff\xfe\x00bad",
    "a nul byte": b'{"code": "invalid_request\x00"}',
}


@pytest.mark.parametrize("body", BAD_ENVELOPES.values(), ids=list(BAD_ENVELOPES))
def test_a_malformed_envelope_never_crashes_the_mapper(body):
    exc = error_from_response(403, body, {})
    assert isinstance(exc, HexReadError)
    assert isinstance(exc.code, str)
    assert isinstance(exc.type, str)
    assert isinstance(exc.request_id, str)
    assert str(exc)


def test_a_null_envelope_keeps_the_status_mapping():
    exc = error_from_response(402, b'{"type": null, "code": null}', {})
    assert isinstance(exc, QuotaError)


def test_extra_envelope_fields_are_ignored_not_reflected():
    exc = error_from_response(429, BAD_ENVELOPES["unexpected extras"], {})
    assert isinstance(exc, QuotaError)
    assert "deeper" not in str(exc)


def test_a_megabyte_of_html_does_not_end_up_in_the_message():
    body = ("<html>" + "spam " * 200_000 + "</html>").encode()
    exc = error_from_response(502, body, {})

    assert isinstance(exc, CapacityError)
    assert len(str(exc)) < 500


def test_a_megabyte_of_html_from_the_server_is_summarised(make_api, make_client):
    body = "<html>" + ("x" * 2_000_000) + "</html>"
    api = make_api(convert=httpx.Response(200, text=body))
    with pytest.raises(ServerError) as caught:
        make_client(api).convert(b"%PDF-1.7")
    assert len(str(caught.value)) < 500


@pytest.mark.parametrize("code", ["", "not_a_real_code", "\x00", "x" * 500])
def test_an_unknown_job_error_code_is_treated_as_transient(code):
    exc = error_from_job_code(code, "job_1")
    assert isinstance(exc, CapacityError)
    assert "job_1" in str(exc)


# -- Retry-After, which the server controls -----------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "-1",
        "-99999",
        "0",
        "0.0",
        "nan",
        "-nan",
        "inf",
        "-inf",
        "999999999",
        "1e308",
        "Wed, 21 Oct 2026 07:28:00 GMT",
        "soon",
        "",
        "  30  ",
        "5, 10",
    ],
)
def test_no_retry_after_can_make_us_sleep_out_of_range(header):
    exc = error_from_response(429, b'{"code": "rate_limited"}', {"retry-after": header})
    delay = retry_delay(0, exc)

    assert 0.0 <= delay <= MAX_BACKOFF
    # The real sleep, capped so the suite stays fast. A negative delay still raises here, which is
    # exactly the production crash this guards.
    time.sleep(min(delay, 0.001))


def test_a_negative_retry_after_is_not_handed_to_the_caller_either():
    exc = error_from_response(429, b"{}", {"retry-after": "-30"})
    assert exc.retry_after == 0.0


def test_an_absurd_retry_after_is_capped_not_honoured():
    exc = error_from_response(503, b"{}", {"retry-after": "86400"})
    assert exc.retry_after == 86400.0, "the server's hint is reported as sent"
    assert retry_delay(0, exc) == MAX_BACKOFF, "but never actually waited for"


def test_a_negative_retry_after_does_not_crash_a_real_conversion(make_api, make_client, sleeps):
    api = make_api(
        convert=error_response(429, type="rate_limit_error", code="rate_limited", retry_after=-5)
    )
    with pytest.raises(RateLimitError):
        make_client(api, max_retries=2).convert(b"%PDF-1.7")

    assert all(delay >= 0 for delay in sleeps)


async def test_a_negative_retry_after_does_not_crash_an_async_conversion(
    make_api, make_async_client, async_sleeps
):
    api = make_api(
        convert=error_response(429, type="rate_limit_error", code="rate_limited", retry_after=-5)
    )
    async with make_async_client(api, max_retries=2) as client:
        with pytest.raises(RateLimitError):
            await client.convert(b"%PDF-1.7")

    assert all(delay >= 0 for delay in async_sleeps)


# -- retry storms ------------------------------------------------------------------------------


@pytest.mark.parametrize("retry_after", [None, 1, 3600, "-5"])
def test_a_server_that_always_429s_is_given_up_on(make_api, make_client, sleeps, retry_after):
    api = make_api(
        convert=error_response(
            429, type="rate_limit_error", code="rate_limited", retry_after=retry_after
        )
    )
    with pytest.raises(RateLimitError):
        make_client(api).convert(b"%PDF-1.7")

    assert len(api.sent("convert")) == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES
    assert sum(sleeps) <= MAX_BACKOFF * MAX_RETRIES


def test_max_retries_zero_means_one_attempt(make_api, make_client, sleeps):
    api = make_api(convert=error_response(503, type="capacity_error", code="conversion_failed"))
    with pytest.raises(CapacityError):
        make_client(api, max_retries=0).convert(b"%PDF-1.7")

    assert len(api.sent("convert")) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 410, 413, 422])
def test_a_failure_a_retry_cannot_fix_is_not_retried(make_api, make_client, sleeps, status):
    api = make_api(convert=httpx.Response(status, json={"code": ""}))
    with pytest.raises(HexReadError):
        make_client(api).convert(b"%PDF-1.7")

    assert len(api.sent("convert")) == 1
    assert sleeps == []


def test_the_idempotency_key_is_stable_across_retries(make_api, make_client, sleeps):
    api = make_api(
        convert=[
            error_response(429, type="rate_limit_error", code="rate_limited"),
            error_response(503, type="capacity_error", code="conversion_failed"),
            ok_response(),
        ]
    )
    make_client(api).convert(b"%PDF-1.7")

    keys = {request.headers["idempotency-key"] for request in api.sent("convert")}
    assert len(api.sent("convert")) == 3
    assert len(keys) == 1, "a retry that changes the key can be billed twice"


def test_every_call_gets_its_own_idempotency_key(make_api, make_client):
    api = make_api(convert=ok_response())
    client = make_client(api)
    for _ in range(50):
        client.convert(b"%PDF-1.7")

    keys = [request.headers["idempotency-key"] for request in api.sent("convert")]
    assert len(set(keys)) == 50, "a shared key would collide two conversions on the server"
    assert all(key.startswith("py-") and len(key) == 35 for key in keys)


def test_a_caller_supplied_idempotency_key_is_used_verbatim(make_api, make_client, sleeps):
    api = make_api(
        convert=[
            error_response(503, type="capacity_error", code="conversion_failed"),
            ok_response(),
        ]
    )
    make_client(api).convert(b"%PDF-1.7", idempotency_key="mine-42")

    assert {r.headers["idempotency-key"] for r in api.sent("convert")} == {"mine-42"}


def test_a_retry_re_sends_the_whole_body(make_api, make_client, sleeps):
    api = make_api(
        convert=[
            error_response(503, type="capacity_error", code="conversion_failed"),
            ok_response(),
        ]
    )
    make_client(api).convert(b"%PDF-1.7" + b"A" * 4096)

    first, second = api.sent("convert")
    assert len(first.content) == len(second.content)


def test_an_error_carries_the_request_id_into_its_message():
    exc = error_from_response(500, b'{"request_id": "req_abc", "code": "internal_error"}', {})
    assert "req_abc" in str(exc)
