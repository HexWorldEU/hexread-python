"""Retry policy: what is retried, how long it waits, and what it must not do twice.

`sleeps` swaps time.sleep for a recorder, so these run instantly and the backoff is assertable.
"""

from __future__ import annotations

import pytest
from conftest import PDF_BYTES, error_response, file_part, ok_response

from hexread import (
    AuthenticationError,
    CapacityError,
    ConflictError,
    PayloadTooLargeError,
    PermissionError,
    QuotaError,
    RateLimitError,
    ValidationError,
)
from hexread._transport import MAX_BACKOFF, retry_delay


def keys_of(api):
    return [request.headers["idempotency-key"] for request in api.sent("convert")]


def test_a_rate_limit_is_retried_and_then_succeeds(make_api, make_client, pdf, sleeps):
    api = make_api(
        convert=[
            error_response(429, type="rate_limit_error", code="rate_limited", retry_after=2),
            ok_response(),
        ]
    )
    result = make_client(api).convert(pdf)

    assert result.page_count == 2
    assert len(api.sent("convert")) == 2
    assert sleeps == [2.0], "Retry-After decides the wait"


def test_a_retry_reuses_the_same_idempotency_key(make_api, make_client, pdf, sleeps):
    """A retry must be the same request, or the server bills the conversion twice."""
    api = make_api(
        convert=[
            error_response(429, type="rate_limit_error", code="rate_limited", retry_after=1),
            ok_response(),
        ]
    )
    make_client(api).convert(pdf)

    first, second = keys_of(api)
    assert first == second
    assert first.startswith("py-")


def test_a_retry_re_sends_the_whole_body(make_api, make_client, pdf, sleeps):
    api = make_api(convert=[error_response(503, type="capacity_error"), ok_response()])
    make_client(api).convert(pdf)

    sent = [file_part(request).value for request in api.sent("convert")]
    assert sent == [PDF_BYTES, PDF_BYTES]


def test_a_server_error_is_retried_and_then_succeeds(make_api, make_client, pdf, sleeps):
    api = make_api(convert=[error_response(500, type="internal_error"), ok_response()])
    result = make_client(api).convert(pdf)

    assert result.markdown.startswith("# Quarterly report")
    assert len(api.sent("convert")) == 2
    assert sleeps == [0.5], "no Retry-After, so the first backoff step applies"


def test_an_idempotency_conflict_in_progress_is_retried(make_api, make_client, pdf, sleeps):
    api = make_api(
        convert=[
            error_response(409, type="conflict", code="idempotency_in_progress"),
            ok_response(),
        ]
    )
    assert make_client(api).convert(pdf).page_count == 2
    assert len(api.sent("convert")) == 2


def test_a_validation_error_is_not_retried(make_api, make_client, pdf, sleeps):
    api = make_api(
        convert=error_response(400, type="validation_error", code="invalid_request", message="bad")
    )
    with pytest.raises(ValidationError):
        make_client(api).convert(pdf)

    assert len(api.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("status", "etype", "expected"),
    [
        (401, "authentication_error", AuthenticationError),
        (403, "permission_error", PermissionError),
        (402, "quota_error", QuotaError),
        (413, "payload_too_large", PayloadTooLargeError),
        (409, "conflict", ConflictError),
    ],
)
def test_failures_a_second_attempt_cannot_fix_are_raised_at_once(
    make_api, make_client, pdf, sleeps, status, etype, expected
):
    api = make_api(convert=error_response(status, type=etype))
    with pytest.raises(expected):
        make_client(api).convert(pdf)

    assert len(api.requests) == 1
    assert sleeps == []


def test_retries_stop_at_max_retries_and_the_last_error_is_raised(
    make_api, make_client, pdf, sleeps
):
    api = make_api(convert=error_response(503, type="capacity_error", code="conversion_failed"))
    with pytest.raises(CapacityError) as caught:
        make_client(api, max_retries=2).convert(pdf)

    assert caught.value.status == 503
    assert len(api.requests) == 3, "the first attempt plus two retries"
    assert sleeps == [0.5, 1.0]


def test_max_retries_zero_never_retries(make_api, make_client, pdf, sleeps):
    api = make_api(convert=error_response(429, type="rate_limit_error", code="rate_limited"))
    with pytest.raises(RateLimitError):
        make_client(api, max_retries=0).convert(pdf)

    assert len(api.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("attempt", "hinted", "expected"),
    [
        (0, None, 0.5),
        (1, None, 1.0),
        (2, None, 2.0),
        (7, None, MAX_BACKOFF),
        (0, 3, 3.0),
        (0, 9000, MAX_BACKOFF),
    ],
)
def test_retry_delay_honours_retry_after_and_stays_capped(attempt, hinted, expected):
    exc = RateLimitError("slow down", status=429, retry_after=hinted)
    assert retry_delay(attempt, exc) == expected


def test_a_concurrency_limit_is_retried(make_api, make_client, pdf, sleeps):
    """A 429 concurrency_limit clears as soon as an in-flight conversion finishes, so it retries.

    It arrives as a capacity_error rather than a rate_limit_error while carrying the same 429. An
    earlier version keyed the retry on the class and skipped it, which broke batching on any plan
    whose concurrency is one.
    """
    api = make_api(
        convert=[
            error_response(
                429,
                type="capacity_error",
                code="concurrency_limit",
                message="Too many conversions in flight for your tier.",
                retry_after=2,
            ),
            ok_response(),
        ]
    )
    client = make_client(api)

    result = client.convert(pdf)

    assert result.markdown
    assert len(api.sent("convert")) == 2
    assert len(set(keys_of(api))) == 1, "a retry must reuse the same idempotency key"
    assert sleeps == [2.0], "the server's Retry-After should be honoured"
