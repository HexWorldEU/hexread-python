"""Shared request construction and retry policy.

Pure: no I/O, so the sync and async clients stay in step.
"""

from __future__ import annotations

import contextlib
import math
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

from ._errors import (
    HexReadError,
    ResultDeliveryError,
    ServerError,
    error_from_response,
    is_retryable,
)
from ._models import ConvertResult

USER_AGENT = "hexread-python"

# A sync convert can block a while before the server hands back a job instead, so the read
# budget has to outlast that.
CONVERT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=None, pool=10.0)
CONTROL_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
# The stream sends periodic keepalives, so a long silence means it is gone.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=None, pool=10.0)

MAX_RETRIES = 4
MAX_BACKOFF = 30.0

# Only reconnects that learn nothing count, so a slow job on a flaky link survives.
SSE_MAX_STALLED_RETRIES = 5
SSE_RETRY_BASE = 1.0
SSE_MAX_TOTAL_SECONDS = 2 * 60 * 60


def new_idempotency_key() -> str:
    """A fresh key for one conversion.

    `secrets`, never `random`: the key is a replay capability, and a predictable one would let
    another caller collide with an in-flight conversion.
    """
    return "py-" + secrets.token_hex(16)


def auth_headers(api_key: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


@dataclass(slots=True)
class Upload:
    """A file ready to be posted, plus the display name used in progress and metadata."""

    name: str
    content: bytes | BinaryIO
    close: Any = None

    def as_files(self) -> dict[str, tuple[str, Any, str]]:
        # The server sniffs the real type from magic bytes; the declared one only has to be there.
        return {"file": (self.name, self.content, "application/octet-stream")}

    def release(self) -> None:
        """Close the handle this library opened, if it opened one.

        A file object the caller passed in is theirs and is left open. Nothing raised here escapes:
        this runs in a `finally`, where an exception would replace the failure being reported.
        """
        if not self.close:
            return
        with contextlib.suppress(OSError):
            self.close()

    def rewind(self) -> bool:
        """Put the body back at the start so a retry re-sends all of it.

        False means it cannot be rewound, and the caller must not retry: a stream that has already
        been read to the end would re-upload as an empty document, which the server accepts and
        bills as a conversion of nothing.
        """
        if isinstance(self.content, (bytes, bytearray, memoryview)):
            return True
        seek = getattr(self.content, "seek", None)
        if not callable(seek):
            return False
        try:
            seek(0)
        except (OSError, ValueError):
            return False
        return True


def prepare_upload(source: Any, *, name: str | None = None) -> Upload:
    """Accept a path, raw bytes, or an open binary file, and normalise it for multipart."""
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"hexread: no such file: {path}")
        if path.is_dir():
            raise IsADirectoryError(
                f"hexread: {path} is a directory. Pass a file, or hand its files to convert_many()."
            )
        handle = path.open("rb")
        return Upload(name=name or path.name, content=handle, close=handle.close)
    if isinstance(source, (bytes, bytearray, memoryview)):
        return Upload(name=name or "document", content=bytes(source))
    if hasattr(source, "read"):
        display = name or str(getattr(source, "name", "") or "document")
        return Upload(name=Path(display).name, content=source)
    raise TypeError(
        f"hexread: expected a file path, bytes, or an open binary file, got {type(source).__name__}"
    )


def convert_fields(
    *,
    model: str | None,
    lang: str | None,
    prefer: str | None,
) -> dict[str, str]:
    """Multipart fields beside the file. Omit anything unset: the server has its own defaults."""
    fields: dict[str, str] = {}
    if model:
        fields["model"] = model
    if lang:
        fields["lang"] = lang
    if prefer:
        if prefer not in ("sync", "async"):
            raise ValueError("hexread: prefer must be 'sync' or 'async'")
        fields["prefer"] = prefer
    return fields


def quota_remaining(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("x-hexread-quota-pages-remaining")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def decode_payload(response: httpx.Response) -> dict[str, Any]:
    """Read a success body that the API contract says is a JSON object.

    A 2xx is not proof the body came from HexRead. A captive portal, a corporate proxy, or a CDN
    error page all answer 200 with HTML, and a raw JSONDecodeError or AttributeError escaping from
    here would crash a caller that is correctly catching HexReadError - including convert_many,
    which captures this library's exception type and nothing else.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload
    raise ServerError(
        f"The API answered {response.status_code} with a body this client cannot read as a JSON "
        f"object ({_describe_body(response)}). Something between you and HexRead may be "
        "rewriting responses.",
        status=response.status_code,
        type="internal_error",
        code="invalid_response",
    )


def build_result(
    payload: Any,
    raw: str,
    *,
    source: str,
    pages_remaining: int | None,
    job_id: str = "",
) -> ConvertResult:
    """Turn a decoded body into a result, or fail with the markdown still attached.

    Both delivery paths are read once. An inline body is never stored, and a job result is served
    exactly one time, so a field this client cannot coerce - a page count that is not a number, a
    pages array full of strings - must not take the document down with it. The raw body rides out
    on the exception instead, which is the only remaining copy.
    """
    try:
        return ConvertResult.from_payload(
            payload, source=source, pages_remaining=pages_remaining, job_id=job_id
        )
    except Exception as exc:  # noqa: BLE001 - a read-once body outranks any decoding problem
        raise ResultDeliveryError(
            "The document was converted but the response could not be decoded. It cannot be "
            "fetched again, so the raw body is attached to this exception.",
            markdown=raw,
            job_id=job_id,
        ) from exc


def _describe_body(response: httpx.Response) -> str:
    """A short, safe description of a body that must never be pasted whole into a message."""
    content_type = response.headers.get("content-type", "").partition(";")[0].strip()
    try:
        body = response.text
    except (UnicodeDecodeError, httpx.HTTPError):
        body = ""
    label = content_type or "no content type"
    if not body:
        return f"{label}, empty body"
    return f"{label}, {len(body)} chars starting {body[:80]!r}"


def raise_for_response(response: httpx.Response) -> None:
    """Turn a non-2xx into the right exception class, or return quietly."""
    if response.is_success:
        return
    raise error_from_response(response.status_code, response.content, response.headers)


def retry_delay(attempt: int, exc: BaseException) -> float:
    """Seconds to wait before attempt N+1: Retry-After when the server sent one, else backoff."""
    hinted = getattr(exc, "retry_after", None)
    if hinted:
        try:
            seconds = float(hinted)
        except (TypeError, ValueError):
            seconds = 0.0
        if math.isnan(seconds):
            seconds = 0.0
        # A negative Retry-After would make time.sleep() raise instead of retrying.
        return min(MAX_BACKOFF, max(0.0, seconds))
    return min(MAX_BACKOFF, (2**attempt) * 0.5)


def should_retry(exc: BaseException, attempt: int, max_retries: int = MAX_RETRIES) -> bool:
    """Retry only failures a second identical attempt can resolve.

    Network errors are excluded on purpose: a convert that died mid-upload may still be running
    server side, and a blind retry risks converting and billing the same document twice.
    """
    if attempt >= max_retries:
        return False
    return is_retryable(exc)


def resolve_job_url(origin: str, base_url: str, job_id: str, link: str, suffix: str) -> str:
    """Absolute URL for a job sub-resource.

    A job handle's links are root-relative and already carry the /v1 prefix, so they join against
    the origin. Joining them against the base URL would yield /v1/v1/...

    An absolute link is honoured only when it points back at the configured origin. The events
    request carries the API key in an Authorization header, so following a link to some other host
    on the say-so of a response body would hand the credential to whoever wrote that body. Both
    clients run with follow_redirects=False for the same reason; this closes the other half of it.
    """
    if link:
        if _is_absolute(link):
            return link if _same_origin(link, origin) else f"{base_url}/jobs/{job_id}{suffix}"
        return f"{origin}{link if link.startswith('/') else '/' + link}"
    return f"{base_url}/jobs/{job_id}{suffix}"


def _is_absolute(link: str) -> bool:
    """True for a link that can name a host. Scheme-relative counts: `//host/path` names one."""
    return link.startswith("//") or "://" in link


def _same_origin(link: str, origin: str) -> bool:
    """Scheme and host must both match. A scheme-relative or downgraded link never does."""
    parts = urlsplit(link)
    expected = urlsplit(origin)
    return (parts.scheme.lower(), parts.netloc.lower()) == (
        expected.scheme.lower(),
        expected.netloc.lower(),
    )


def unwrap_result_error(exc: HexReadError, job_id: str) -> HexReadError:
    """Attach the job id to a result-fetch failure so the message is actionable."""
    if job_id and job_id not in exc.message:
        exc.message = f"{exc.message} (job {job_id})"
    return exc
