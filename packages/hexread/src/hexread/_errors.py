"""Exceptions, mapped from the HexRead error envelope `{type, code, message, doc_url, request_id}`.

The class is chosen from `type`, never the HTTP status: an `authentication_error` on a 502 is
still an authentication failure.
"""

from __future__ import annotations

import json
import math
from typing import Any

CONVERT_DOCS_URL = "https://hexread.com/api"


class HexReadError(Exception):
    """Base class for everything this library raises."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        type: str = "",
        code: str = "",
        doc_url: str = "",
        request_id: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        self.doc_url = doc_url
        self.request_id = request_id
        self.retry_after = retry_after

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"({self.code})")
        if self.request_id:
            parts.append(f"[request {self.request_id}]")
        return " ".join(parts)


class AuthenticationError(HexReadError):
    """The API key is missing, malformed, revoked, or rejected."""


class PermissionError(HexReadError):  # noqa: A001 - the name mirrors the error class it maps
    """Authenticated, but not allowed: plan gate, missing scope, or a model outside the plan."""


class QuotaError(HexReadError):
    """The page allowance for the current window is spent. There is no metered overage."""


class RateLimitError(HexReadError):
    """Too many requests, or too many conversions in flight at once. Retryable."""


class ValidationError(HexReadError):
    """The request or the document was rejected: bad field, encrypted PDF, too many pages."""


class NotFoundError(HexReadError):
    """No such job, key, or resource."""


class ConflictError(HexReadError):
    """Idempotency conflict, or a state conflict such as an already active subscription."""


class GoneError(HexReadError):
    """A read-once result was already retrieved, or its retrieval window expired."""


class PayloadTooLargeError(HexReadError):
    """The file, a single page, or the document structure exceeds the plan's caps."""


class CapacityError(HexReadError):
    """A transient server-side failure. Retrying later is reasonable."""


class CanceledError(HexReadError):
    """The job was canceled."""


class ServerError(HexReadError):
    """An internal error on the API side."""


class MissingAPIKeyError(AuthenticationError):
    """No credential could be found. Raised before any request is sent."""


class ResultDeliveryError(HexReadError):
    """A read-once result was fetched but could not be handed to the caller.

    Carries the markdown on `.markdown`: the API cannot serve it a second time.
    """

    def __init__(self, message: str, *, markdown: str, job_id: str) -> None:
        super().__init__(message, code="result_delivery_failed")
        self.markdown = markdown
        self.job_id = job_id


TYPE_EXCEPTIONS: dict[str, type[HexReadError]] = {
    "authentication_error": AuthenticationError,
    "permission_error": PermissionError,
    "quota_error": QuotaError,
    "rate_limit_error": RateLimitError,
    "validation_error": ValidationError,
    "not_found": NotFoundError,
    "conflict": ConflictError,
    "gone": GoneError,
    "payload_too_large": PayloadTooLargeError,
    "capacity_error": CapacityError,
    "canceled_error": CanceledError,
    "internal_error": ServerError,
}

# SSE failures carry a code and no type. The catalog is append-only, so an unknown code here means
# a newer server than client, not a bug.
CODE_TYPES: dict[str, str] = {
    "internal_error": "internal_error",
    "cancelled": "canceled_error",
    "exchange_failed": "authentication_error",
    "invalid_api_key": "authentication_error",
    "invalid_signature": "authentication_error",
    "invalid_state": "authentication_error",
    "nonce_mismatch": "authentication_error",
    "oidc_error": "authentication_error",
    "reauth_required": "authentication_error",
    "unauthenticated": "authentication_error",
    "api_not_available_on_tier": "permission_error",
    "cross_origin_blocked": "permission_error",
    "forbidden": "permission_error",
    "forbidden_tier": "permission_error",
    "model_not_available": "permission_error",
    "signup_required": "permission_error",
    "quota_exceeded": "quota_error",
    "trial_expired": "quota_error",
    "rate_limited": "rate_limit_error",
    "concurrency_limit": "capacity_error",
    "idempotency_key_reused": "conflict",
    "idempotency_in_progress": "conflict",
    "already_subscribed": "conflict",
    "subscription_active": "conflict",
    "org_has_members": "conflict",
    "result_consumed": "gone",
    "job_not_found": "not_found",
    "key_not_found": "not_found",
    "no_billing_account": "not_found",
    "no_subscription": "not_found",
    "not_found": "not_found",
    "disposable_email": "validation_error",
    "encrypted_pdf": "validation_error",
    "malformed_document": "validation_error",
    "invalid_plan": "validation_error",
    "invalid_request": "validation_error",
    "missing_token": "validation_error",
    "too_many_pages": "validation_error",
    "unsupported_media_type": "validation_error",
    "file_too_large": "payload_too_large",
    "page_too_large": "payload_too_large",
    "document_too_complex": "payload_too_large",
    "billing_unavailable": "capacity_error",
    "conversion_failed": "capacity_error",
}

# Replaces the server's message where a caller can actually do something about the failure, in
# terms of this library rather than the wire.
CODE_HINTS: dict[str, str] = {
    "api_not_available_on_tier": (
        "Your HexRead plan does not include API access. The free trial is web only. "
        f"Subscribe to a paid plan at {CONVERT_DOCS_URL} and use a key from your dashboard."
    ),
    "signup_required": (
        "No valid credential was sent. Set HEXREAD_API_KEY, or pass api_key= to HexRead()."
    ),
    "invalid_api_key": (
        "This API key is not valid or has been revoked. Create a new one in your HexRead dashboard "
        "and set HEXREAD_API_KEY."
    ),
    "quota_exceeded": (
        "Your page allowance for this period is used up. Allowances are a hard cap, so this will "
        "not resolve on retry: upgrade the plan or wait for the next period."
    ),
    "trial_expired": (
        "The one time free trial allowance is spent. Subscribe to a paid plan to keep converting."
    ),
    "model_not_available": (
        "That parser is not available on your plan. Use model='auto' (always allowed), or check "
        "client.usage().available_models for the ones you may request."
    ),
    "idempotency_key_reused": (
        "This idempotency key was already used. An inline result is delivered once and is never "
        "stored, so it cannot be replayed. Convert again without the key, or pass prefer='async' "
        "so the result can be fetched by job id."
    ),
    "result_consumed": (
        "This result was already retrieved, or its retrieval window expired. Results are "
        "delivered once and are not retained."
    ),
    "unsupported_media_type": (
        "HexRead converts PDF, PNG, JPEG, TIFF and WebP. The uploaded bytes were none of those."
    ),
    "encrypted_pdf": "This PDF is encrypted. Remove the password protection and convert again.",
}

RETRYABLE_CODES = frozenset({"idempotency_in_progress"})


def _hint(code: str, fallback: str) -> str:
    return CODE_HINTS.get(code) or fallback or f"HexRead request failed ({code or 'no code'})."


def error_from_response(
    status: int,
    body: bytes | str,
    headers: Any = None,
) -> HexReadError:
    """Build an exception from a non-2xx response.

    The body may not be an envelope at all: a proxy or CDN can answer before HexRead sees the
    request, and that must still raise the right class.
    """
    envelope: dict[str, Any] = {}
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        parsed = json.loads(text) if text.strip() else {}
        if isinstance(parsed, dict):
            envelope = parsed
    except ValueError:
        envelope = {}

    code = str(envelope.get("code", "") or "")
    etype = (
        str(envelope.get("type", "") or "") or CODE_TYPES.get(code, "") or _type_for_status(status)
    )
    message = _hint(code, str(envelope.get("message", "") or ""))

    retry_after: float | None = None
    if headers is not None:
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        if raw:
            try:
                retry_after = float(raw)
            except ValueError:
                # An HTTP-date Retry-After, which this client does not parse. Backoff covers it.
                retry_after = None
            else:
                # A negative or NaN hint is nonsense, and callers pass this value straight to
                # sleep(), where a negative raises. 0.0 means "our own backoff decides".
                if math.isnan(retry_after) or retry_after < 0:
                    retry_after = 0.0

    exc_cls = TYPE_EXCEPTIONS.get(etype, HexReadError)
    return exc_cls(
        message,
        status=status,
        type=etype,
        code=code,
        doc_url=str(envelope.get("doc_url", "") or ""),
        request_id=str(envelope.get("request_id", "") or ""),
        retry_after=retry_after,
    )


def error_from_job_code(code: str, job_id: str = "") -> HexReadError:
    """Build an exception from an SSE error event, which carries a code and nothing else."""
    # Unknown codes fall to the transient class: "retry may help" beats "your request was invalid".
    etype = CODE_TYPES.get(code, "capacity_error")
    exc_cls = TYPE_EXCEPTIONS.get(etype, HexReadError)
    suffix = f" (job {job_id})" if job_id else ""
    return exc_cls(
        _hint(code, f"The conversion failed with {code or 'an unknown error'}.") + suffix,
        type=etype,
        code=code,
    )


def is_retryable(exc: BaseException) -> bool:
    """Retry only what can plausibly succeed on a second attempt with the same idempotency key.

    Every 429 qualifies whatever class it wears: `concurrency_limit` is a CapacityError on that
    status, and it clears as soon as one of the account's in-flight conversions finishes.
    """
    if not isinstance(exc, HexReadError):
        return False
    if exc.code in RETRYABLE_CODES:
        return True
    if exc.status == 429:
        return True
    if isinstance(exc, RateLimitError):
        return True
    return exc.status >= 500


def _type_for_status(status: int) -> str:
    if status in (401, 407):
        return "authentication_error"
    if status == 402:
        return "quota_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 410:
        return "gone"
    if status == 413:
        return "payload_too_large"
    if status == 429:
        return "rate_limit_error"
    if 400 <= status < 500:
        return "validation_error"
    if status in (502, 503, 504):
        return "capacity_error"
    return "internal_error"
