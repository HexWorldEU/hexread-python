"""Result types returned by the client, mirroring the HexRead OpenAPI response schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a converted document."""

    index: int
    """Zero-based, as returned by the API."""

    markdown: str

    @property
    def number(self) -> int:
        """The page number a human would write (1-based)."""
        return self.index + 1


@dataclass(frozen=True, slots=True)
class Meta:
    """Conversion metadata: which parser ran, how long it took, how big the document was."""

    pages: int
    sha256: str
    timing_ms: int
    backend: str
    model: str = ""
    route_reason: str = ""
    """Why the auto router picked that model. Empty when a model was requested explicitly."""


@dataclass(frozen=True, slots=True)
class ConvertResult:
    """A converted document. `str(result)` is the full markdown."""

    markdown: str
    pages: list[Page]
    meta: Meta
    source: str = ""
    """Where the input came from (a path or file name). Set by the client, not the API."""

    pages_remaining: int | None = None
    job_id: str = ""
    """Set when the conversion took the async path; empty for an inline conversion."""

    def __str__(self) -> str:
        return self.markdown

    @property
    def model(self) -> str:
        return self.meta.model

    @property
    def route_reason(self) -> str:
        return self.meta.route_reason

    @property
    def page_count(self) -> int:
        return self.meta.pages

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        source: str = "",
        pages_remaining: int | None = None,
        job_id: str = "",
    ) -> ConvertResult:
        raw_meta = payload.get("meta") or {}
        meta = Meta(
            pages=int(raw_meta.get("pages", 0)),
            sha256=str(raw_meta.get("sha256", "")),
            timing_ms=int(raw_meta.get("timing_ms", 0)),
            backend=str(raw_meta.get("backend", "")),
            model=str(raw_meta.get("model", "") or ""),
            route_reason=str(raw_meta.get("route_reason", "") or ""),
        )
        pages = [
            Page(index=int(p.get("index", i)), markdown=str(p.get("markdown", "")))
            for i, p in enumerate(payload.get("pages") or [])
        ]
        return cls(
            markdown=str(payload.get("markdown", "")),
            pages=pages,
            meta=meta,
            source=source,
            pages_remaining=pages_remaining,
            job_id=job_id,
        )


@dataclass(frozen=True, slots=True)
class Usage:
    """Account meters from GET /v1/usage."""

    tier: str
    pages_used: int
    pages_remaining: int
    pages_allowance: int
    pages_reserved: int = 0
    concurrency_in_use: int = 0
    concurrency_limit: int = 0
    api_access: bool = False
    available_models: list[str] = field(default_factory=list)
    """Models this plan may request explicitly. Empty means auto and the default still convert."""

    period: str = ""
    resets_at: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Usage:
        pages = payload.get("pages") or {}
        conc = payload.get("concurrency") or {}
        return cls(
            tier=str(payload.get("tier", "")),
            pages_used=int(pages.get("used", 0)),
            pages_remaining=int(pages.get("remaining", 0)),
            pages_allowance=int(pages.get("allowance", 0)),
            pages_reserved=int(pages.get("reserved", 0)),
            concurrency_in_use=int(conc.get("in_use", 0)),
            concurrency_limit=int(conc.get("limit", 0)),
            api_access=bool(payload.get("api_access", False)),
            available_models=list(payload.get("available_models") or []),
            period=str(payload.get("period", "") or ""),
            resets_at=str(payload.get("resets_at", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class Progress:
    """Conversion progress, passed to an `on_progress` callback."""

    phase: str
    """queued, warming, processing, uploading, or completed."""

    pages_done: int = 0
    pages_total: int = 0
    source: str = ""

    @property
    def fraction(self) -> float:
        """Completed share in 0..1, or 0.0 when the page count is not known yet."""
        if self.pages_total <= 0:
            return 0.0
        return min(1.0, self.pages_done / self.pages_total)
