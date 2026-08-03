"""HexRead: convert PDFs and images to Markdown.

    import hexread

    print(hexread.convert("report.pdf"))

API access requires a paid HexRead plan; the free trial is web only. Create a key at
https://hexread.com and set HEXREAD_API_KEY, or sign in once with the HexRead CLI.
"""

from __future__ import annotations

from typing import Any

from ._async_client import AsyncHexRead
from ._auth import API_KEY_ENV, BASE_URL_ENV, DEFAULT_BASE_URL
from ._client import HexRead
from ._errors import (
    AuthenticationError,
    CanceledError,
    CapacityError,
    ConflictError,
    GoneError,
    HexReadError,
    MissingAPIKeyError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionError,
    QuotaError,
    RateLimitError,
    ResultDeliveryError,
    ServerError,
    ValidationError,
)
from ._models import ConvertResult, Meta, Page, Progress, Usage

__version__ = "0.1.0"

__all__ = [
    "HexRead",
    "AsyncHexRead",
    "convert",
    "convert_many",
    "usage",
    "ConvertResult",
    "Page",
    "Meta",
    "Usage",
    "Progress",
    "HexReadError",
    "AuthenticationError",
    "MissingAPIKeyError",
    "PermissionError",
    "QuotaError",
    "RateLimitError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "GoneError",
    "PayloadTooLargeError",
    "CapacityError",
    "CanceledError",
    "ServerError",
    "ResultDeliveryError",
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "__version__",
]


def convert(source: Any, **kwargs: Any) -> ConvertResult:
    """Convert one document with a throwaway client.

    Takes the same keyword arguments as `HexRead.convert`. For more than a document or two, build
    one `HexRead()` and reuse it so the connection is kept.
    """
    client_kwargs = {
        key: kwargs.pop(key)
        for key in ("api_key", "base_url", "timeout", "max_retries")
        if key in kwargs
    }
    with HexRead(**client_kwargs) as client:
        return client.convert(source, **kwargs)


def convert_many(sources: Any, **kwargs: Any) -> list[Any]:
    """Convert several documents with a throwaway client. See `HexRead.convert_many`."""
    client_kwargs = {
        key: kwargs.pop(key)
        for key in ("api_key", "base_url", "timeout", "max_retries")
        if key in kwargs
    }
    with HexRead(**client_kwargs) as client:
        return client.convert_many(sources, **kwargs)


def usage(**kwargs: Any) -> Usage:
    """Read the account's current page allowance and limits."""
    with HexRead(**kwargs) as client:
        return client.usage()
