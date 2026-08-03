"""The inline conversion path: what comes back, and what goes out on the wire."""

from __future__ import annotations

import io

import httpx
import pytest
from conftest import (
    API_KEY,
    BASE_URL,
    META,
    PDF_BYTES,
    convert_payload,
    error_response,
    file_part,
    form_fields,
    ok_response,
    usage_response,
)

import hexread
from hexread import HexRead, Page, QuotaError


def test_inline_conversion_returns_markdown_pages_and_meta(make_api, make_client, pdf):
    api = make_api(convert=ok_response(pages_remaining=498))
    result = make_client(api).convert(pdf)

    assert result.markdown.startswith("# Quarterly report")
    assert str(result) == result.markdown
    assert result.pages == [
        Page(index=0, markdown="# Quarterly report"),
        Page(index=1, markdown="Revenue rose by 12 percent."),
    ]
    assert result.meta.model == META["model"]
    assert result.model == "mineru-2.5"
    assert result.route_reason == "auto: text-light pdf"
    assert result.page_count == 2
    assert result.meta.backend == "vlm"
    assert result.meta.sha256 == "9f2c4a1d"
    assert result.pages_remaining == 498
    assert result.source == "report.pdf"
    assert result.job_id == ""


def test_pages_remaining_is_none_without_the_quota_header(make_api, make_client, pdf):
    api = make_api(convert=ok_response(pages_remaining=None))
    assert make_client(api).convert(pdf).pages_remaining is None


def test_request_carries_bearer_token_and_file_part(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    make_client(api).convert(pdf)

    request = api.only("convert")
    assert request.method == "POST"
    assert str(request.url) == "https://api.hexread.com/v1/convert"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "hexread-python"

    part = file_part(request)
    assert part.name == "file"
    assert part.filename == "report.pdf"
    assert part.value == PDF_BYTES


def test_idempotency_key_is_present_and_unique_per_call(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    client = make_client(api)
    client.convert(pdf)
    client.convert(pdf)

    keys = [request.headers["idempotency-key"] for request in api.sent("convert")]
    assert all(key.startswith("py-") for key in keys)
    assert len(set(keys)) == 2


def test_caller_supplied_idempotency_key_is_sent_verbatim(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    make_client(api).convert(pdf, idempotency_key="mine-1")
    assert api.only("convert").headers["idempotency-key"] == "mine-1"


def test_model_lang_and_prefer_are_omitted_when_unset(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    make_client(api).convert(pdf)
    assert form_fields(api.only("convert")) == {}


def test_model_lang_and_prefer_are_sent_when_set(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    make_client(api).convert(pdf, model="granite-docling", lang="de", prefer="sync")
    assert form_fields(api.only("convert")) == {
        "model": "granite-docling",
        "lang": "de",
        "prefer": "sync",
    }


def test_client_level_model_and_lang_apply_to_every_call(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    client = make_client(api, model="mineru-2.5", lang="fr")
    client.convert(pdf)
    client.convert(pdf, model="paddleocr-vl")

    first, second = (form_fields(request) for request in api.sent("convert"))
    assert first == {"model": "mineru-2.5", "lang": "fr"}
    assert second == {"model": "paddleocr-vl", "lang": "fr"}


def test_prefer_rejects_an_unknown_value(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    with pytest.raises(ValueError, match="prefer must be"):
        make_client(api).convert(pdf, prefer="whenever")
    assert api.requests == []


def test_accepts_a_path(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    make_client(api).convert(str(pdf))

    part = file_part(api.only("convert"))
    assert part.filename == "report.pdf"
    assert part.value == PDF_BYTES


def test_accepts_raw_bytes(make_api, make_client):
    api = make_api(convert=ok_response())
    make_client(api).convert(PDF_BYTES)

    part = file_part(api.only("convert"))
    assert part.filename == "document"
    assert part.value == PDF_BYTES


def test_accepts_an_open_file_handle(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    with pdf.open("rb") as handle:
        make_client(api).convert(handle)
        assert not handle.closed, "the client must not close a handle it did not open"

    part = file_part(api.only("convert"))
    assert part.filename == "report.pdf"
    assert part.value == PDF_BYTES


def test_accepts_an_in_memory_stream_with_an_explicit_name(make_api, make_client):
    api = make_api(convert=ok_response())
    make_client(api).convert(io.BytesIO(PDF_BYTES), name="scan.pdf")

    part = file_part(api.only("convert"))
    assert part.filename == "scan.pdf"
    assert part.value == PDF_BYTES


def test_name_overrides_the_display_name_of_a_path(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    result = make_client(api).convert(pdf, name="renamed.pdf")
    assert file_part(api.only("convert")).filename == "renamed.pdf"
    assert result.source == "renamed.pdf"


def test_a_directory_raises_is_a_directory(make_api, make_client, tmp_path):
    api = make_api(convert=ok_response())
    with pytest.raises(IsADirectoryError, match="convert_many"):
        make_client(api).convert(tmp_path)
    assert api.requests == []


def test_a_missing_path_raises_file_not_found(make_api, make_client, tmp_path):
    api = make_api(convert=ok_response())
    with pytest.raises(FileNotFoundError, match="no such file"):
        make_client(api).convert(tmp_path / "absent.pdf")
    assert api.requests == []


def test_an_unsupported_source_type_raises_type_error(make_api, make_client):
    api = make_api(convert=ok_response())
    with pytest.raises(TypeError, match="file path, bytes"):
        make_client(api).convert(42)
    assert api.requests == []


def test_progress_reaches_completed_on_the_inline_path(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    seen = []
    make_client(api).convert(pdf, on_progress=seen.append)

    assert [update.phase for update in seen] == ["uploading", "completed"]
    assert seen[-1].pages_done == 2
    assert seen[-1].pages_total == 2
    assert seen[-1].source == "report.pdf"


def test_usage_reports_the_account_meters(make_api, make_client):
    api = make_api(usage=usage_response())
    usage = make_client(api).usage()

    assert usage.tier == "pro"
    assert (usage.pages_used, usage.pages_remaining, usage.pages_allowance) == (102, 2398, 2500)
    assert usage.pages_reserved == 3
    assert (usage.concurrency_in_use, usage.concurrency_limit) == (1, 4)
    assert usage.api_access is True
    assert "granite-docling" in usage.available_models
    assert api.only("usage").headers["authorization"] == f"Bearer {API_KEY}"


def test_usage_raises_on_an_error_envelope(make_api, make_client):
    api = make_api(usage=error_response(402, type="quota_error", code="quota_exceeded"))
    with pytest.raises(QuotaError):
        make_client(api).usage()


def test_cancel_deletes_the_job(make_api, make_client):
    api = make_api(cancel=httpx.Response(204))
    make_client(api).cancel("job_1")

    request = api.only("cancel")
    assert request.method == "DELETE"
    assert str(request.url) == "https://api.hexread.com/v1/jobs/job_1"


def test_convert_many_keeps_input_order_and_captures_failures(make_api, make_client, tmp_path):
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(PDF_BYTES)
    second.write_bytes(PDF_BYTES)

    def by_name(request: httpx.Request) -> httpx.Response:
        name = file_part(request).filename
        if name == "b.pdf":
            return error_response(402, type="quota_error", code="quota_exceeded")
        return ok_response(convert_payload(markdown=f"# {name}"))

    api = make_api(convert=by_name)
    results = make_client(api).convert_many([first, second], max_workers=2)

    assert len(results) == 2
    assert results[0].markdown == "# a.pdf"
    assert isinstance(results[1], QuotaError)


def test_convert_many_can_raise_instead_of_collecting(make_api, make_client, pdf):
    api = make_api(convert=error_response(402, type="quota_error", code="quota_exceeded"))
    with pytest.raises(QuotaError):
        make_client(api).convert_many([pdf], max_workers=1, raise_on_error=True)


def test_convert_many_with_no_sources_sends_nothing(make_api, make_client):
    api = make_api(convert=ok_response())
    assert make_client(api).convert_many([]) == []
    assert api.requests == []


def test_context_manager_closes_the_http_client(make_api):
    api = make_api(convert=ok_response())
    with HexRead(api_key=API_KEY, base_url=BASE_URL, transport=api.transport) as client:
        assert client.convert(PDF_BYTES).page_count == 2
    with pytest.raises(RuntimeError, match="closed"):
        client.convert(PDF_BYTES)


def test_module_level_convert_splits_client_and_call_arguments(monkeypatch, pdf):
    """`hexread.convert` builds a throwaway client, converts once, and closes it."""
    seen: dict[str, object] = {}

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            seen["client"] = kwargs
            seen["closed"] = False

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *exc_info: object) -> None:
            seen["closed"] = True

        def convert(self, source: object, **kwargs: object) -> str:
            seen["source"] = source
            seen["convert"] = kwargs
            return "converted"

    monkeypatch.setattr(hexread, "HexRead", RecordingClient)
    out = hexread.convert(pdf, api_key="k", base_url="https://example.test/v1", model="auto")

    assert out == "converted"
    assert seen["client"] == {"api_key": "k", "base_url": "https://example.test/v1"}
    assert seen["convert"] == {"model": "auto"}
    assert seen["source"] == pdf
    assert seen["closed"] is True
