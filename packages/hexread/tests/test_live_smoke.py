"""Smoke tests against the real API.

Two environment variables have to be set, so `pytest` on a laptop or in CI stays offline:

    HEXREAD_API_KEY         a key on a plan with API access
    HEXREAD_TEST_DOCS       a directory holding at least one .pdf to convert

Run them deliberately with `pytest -m live`. The budget is three conversions: two inline and one
over the job path, which is the only way to exercise SSE and the read-once token against
production. Pages are billed, so do not add a fourth without a reason.

No document is committed here. Point HEXREAD_TEST_DOCS at whatever PDFs you have; the tests assert
on the shape of the result, never on its content.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hexread import HexRead
from hexread._auth import API_KEY_ENV

DOCS_ENV = "HEXREAD_TEST_DOCS"
_docs = os.environ.get(DOCS_ENV, "").strip()
DOCS = Path(_docs) if _docs else None
BENCH_DOCS = sorted(DOCS.glob("*.pdf"))[:2] if DOCS is not None and DOCS.is_dir() else []

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get(API_KEY_ENV, "").strip(),
        reason=f"set {API_KEY_ENV} to run the live smoke tests",
    ),
    pytest.mark.skipif(
        not BENCH_DOCS, reason=f"set {DOCS_ENV} to a directory holding at least one .pdf"
    ),
]


@pytest.fixture(scope="module")
def live_client():
    with HexRead() as client:
        yield client


def test_usage_reports_a_plan_with_api_access(live_client):
    """A 403 here means the key is on a plan without API access, not that the SDK is broken."""
    usage = live_client.usage()

    assert usage.api_access is True
    assert usage.pages_remaining > 0, "no page allowance left, the conversions below would fail"


@pytest.mark.parametrize("document", BENCH_DOCS, ids=lambda path: path.name)
def test_converts_a_bench_document(live_client, document):
    result = live_client.convert(document)

    assert result.markdown.strip(), "the API returned empty markdown"
    assert result.model, "the API returned no model id"
    assert result.page_count >= 1
    assert result.source == document.name


def test_the_job_path_delivers_markdown(live_client):
    """prefer='async' forces the 202, the event stream, and the one-time result token."""
    phases: list[str] = []
    result = live_client.convert(
        BENCH_DOCS[0], prefer="async", on_progress=lambda update: phases.append(update.phase)
    )

    assert result.markdown.strip()
    assert result.model
    assert result.job_id, "a job conversion must report the job id it came from"
    assert "completed" in phases
