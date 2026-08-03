"""Envelope to exception mapping, and the hints that replace a server message."""

from __future__ import annotations

import json

import pytest
from conftest import error_response

from hexread import (
    AuthenticationError,
    CanceledError,
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
from hexread._errors import (
    CODE_HINTS,
    CODE_TYPES,
    TYPE_EXCEPTIONS,
    error_from_job_code,
    error_from_response,
    is_retryable,
)

# Every type in the envelope contract, with a status the API actually pairs it with.
ENVELOPE_TYPES = [
    ("authentication_error", 401, AuthenticationError),
    ("permission_error", 403, PermissionError),
    ("quota_error", 402, QuotaError),
    ("rate_limit_error", 429, RateLimitError),
    ("validation_error", 400, ValidationError),
    ("not_found", 404, NotFoundError),
    ("conflict", 409, ConflictError),
    ("gone", 410, GoneError),
    ("payload_too_large", 413, PayloadTooLargeError),
    ("capacity_error", 503, CapacityError),
    ("canceled_error", 409, CanceledError),
    ("internal_error", 500, ServerError),
]


def envelope(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def test_the_table_covers_every_type_the_library_maps():
    assert {name for name, _, _ in ENVELOPE_TYPES} == set(TYPE_EXCEPTIONS)
    assert len(ENVELOPE_TYPES) == 12


@pytest.mark.parametrize(("etype", "status", "expected"), ENVELOPE_TYPES)
def test_each_envelope_type_maps_to_its_exception(etype, status, expected):
    exc = error_from_response(status, envelope(type=etype, code="", message="nope"))

    assert type(exc) is expected
    assert isinstance(exc, HexReadError)
    assert exc.type == etype
    assert exc.status == status
    assert exc.message == "nope"


@pytest.mark.parametrize(("etype", "status", "expected"), ENVELOPE_TYPES)
def test_the_type_wins_over_the_status(etype, status, expected):
    """A gateway can move the status; the envelope type is the contract."""
    assert type(error_from_response(502, envelope(type=etype))) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (402, QuotaError),
        (403, PermissionError),
        (404, NotFoundError),
        (409, ConflictError),
        (410, GoneError),
        (413, PayloadTooLargeError),
        (422, ValidationError),
        (429, RateLimitError),
        (500, ServerError),
        (502, CapacityError),
        (504, CapacityError),
    ],
)
def test_a_non_json_body_still_raises_the_class_the_status_implies(status, expected):
    """A proxy can answer before HexRead sees the request, with HTML and no envelope."""
    body = b"<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    exc = error_from_response(status, body)

    assert type(exc) is expected
    assert exc.status == status
    assert exc.code == ""
    assert exc.message == "HexRead request failed (no code)."


def test_an_empty_body_still_raises():
    assert type(error_from_response(500, b"")) is ServerError


def test_a_json_body_that_is_not_an_object_still_raises():
    assert type(error_from_response(429, b'["rate limited"]')) is RateLimitError


@pytest.mark.parametrize(
    "code",
    [
        "api_not_available_on_tier",
        "quota_exceeded",
        "model_not_available",
        "idempotency_key_reused",
    ],
)
def test_the_friendly_hint_replaces_the_server_message(code):
    exc = error_from_response(403, envelope(code=code, message="server side wording"))

    assert exc.message == CODE_HINTS[code]
    assert "server side wording" not in exc.message
    assert type(exc) is TYPE_EXCEPTIONS[CODE_TYPES[code]]


def test_a_code_without_a_hint_keeps_the_server_message():
    exc = error_from_response(400, envelope(code="malformed_document", message="page 3 is broken"))
    assert exc.message == "page 3 is broken"


def test_the_code_alone_decides_the_type_when_the_envelope_omits_it():
    exc = error_from_response(400, envelope(code="encrypted_pdf"))
    assert type(exc) is ValidationError
    assert exc.type == "validation_error"


def test_the_exception_carries_status_code_request_id_and_doc_url():
    exc = error_from_response(
        413,
        envelope(
            type="payload_too_large",
            code="file_too_large",
            message="too big",
            doc_url="https://hexread.com/api#limits",
            request_id="req_9f2c",
        ),
        headers={"retry-after": "12"},
    )

    assert exc.status == 413
    assert exc.code == "file_too_large"
    assert exc.request_id == "req_9f2c"
    assert exc.doc_url == "https://hexread.com/api#limits"
    assert exc.retry_after == 12.0
    assert str(exc) == "too big (file_too_large) [request req_9f2c]"


def test_a_non_numeric_retry_after_is_ignored():
    exc = error_from_response(
        429, envelope(type="rate_limit_error"), headers={"retry-after": "soon"}
    )
    assert exc.retry_after is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("encrypted_pdf", ValidationError),
        ("cancelled", CanceledError),
        ("result_consumed", GoneError),
        ("quota_exceeded", QuotaError),
        ("conversion_failed", CapacityError),
        ("concurrency_limit", CapacityError),
        ("job_not_found", NotFoundError),
        ("", CapacityError),
        ("a_code_from_a_newer_server", CapacityError),
    ],
)
def test_error_from_job_code_maps_known_codes_and_defaults_the_rest(code, expected):
    exc = error_from_job_code(code, "job_7")

    assert type(exc) is expected
    assert exc.code == code
    assert "(job job_7)" in exc.message


def test_error_from_job_code_omits_the_suffix_without_a_job_id():
    assert "job" not in error_from_job_code("conversion_failed").message


def test_every_registry_code_maps_to_a_type_that_exists():
    assert set(CODE_TYPES.values()) <= set(TYPE_EXCEPTIONS)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RateLimitError("", status=429), True),
        (ConflictError("", status=409, code="idempotency_in_progress"), True),
        (ServerError("", status=500), True),
        (CapacityError("", status=503), True),
        (ValidationError("", status=400), False),
        (ConflictError("", status=409, code="idempotency_key_reused"), False),
        (QuotaError("", status=402), False),
        (ValueError("not ours"), False),
    ],
)
def test_is_retryable_only_accepts_failures_a_second_attempt_can_fix(exc, expected):
    assert is_retryable(exc) is expected


def test_the_client_raises_the_mapped_class_for_a_convert_failure(make_api, make_client, pdf):
    api = make_api(
        convert=error_response(
            403, type="permission_error", code="api_not_available_on_tier", message="forbidden"
        )
    )
    with pytest.raises(PermissionError) as caught:
        make_client(api).convert(pdf)

    assert caught.value.message == CODE_HINTS["api_not_available_on_tier"]
    assert caught.value.status == 403
    assert caught.value.request_id == "req_test"
    assert len(api.requests) == 1
