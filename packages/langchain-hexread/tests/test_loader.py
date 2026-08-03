"""Loader tests.

The API is served by httpx.MockTransport, so no request leaves the process and no key is needed.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import httpx
import pytest

pytest.importorskip("langchain_core")

from hexread import AsyncHexRead, HexRead  # noqa: E402

from langchain_hexread import HexReadLoader  # noqa: E402

BASE_URL = "https://api.test/v1"


def _uploaded_name(request: httpx.Request) -> str:
    """The filename from the multipart body, so a response can name the file it answers."""
    match = re.search(r'filename="([^"]+)"', request.content.decode("utf-8", "replace"))
    return match.group(1) if match else "unknown"


def _mock(
    *,
    pages: int = 3,
    route_reason: str = "auto",
    record: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """A transport that answers /convert inline with `pages` pages of markdown."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        name = _uploaded_name(request)
        body = [{"index": i, "markdown": f"{name} page {i + 1}"} for i in range(pages)]
        return httpx.Response(
            200,
            json={
                "markdown": "\n\n".join(page["markdown"] for page in body),
                "pages": body,
                "meta": {
                    "pages": pages,
                    "sha256": "0" * 64,
                    "timing_ms": 42,
                    "backend": "test",
                    "model": "mineru-2.5",
                    "route_reason": route_reason,
                },
            },
        )

    return httpx.MockTransport(handler)


def _client(**kwargs: object) -> HexRead:
    return HexRead(api_key="hr_test", base_url=BASE_URL, transport=_mock(**kwargs))


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    return path


def test_page_split_metadata_and_count(pdf):
    loader = HexReadLoader(pdf, client=_client(pages=3), extra_metadata={"collection": "q3"})
    docs = loader.load()

    assert len(docs) == 3
    assert [d.page_content for d in docs] == [f"report.pdf page {n}" for n in (1, 2, 3)]
    assert [d.metadata["page"] for d in docs] == [0, 1, 2]
    assert [d.metadata["page_label"] for d in docs] == ["1", "2", "3"]

    first = docs[0].metadata
    assert first["source"] == str(pdf)
    assert first["total_pages"] == 3
    assert first["model"] == "mineru-2.5"
    assert first["route_reason"] == "auto"
    assert first["parser"] == "hexread"
    assert first["collection"] == "q3"


def test_route_reason_omitted_when_empty(pdf):
    docs = HexReadLoader(pdf, client=_client(pages=1, route_reason="")).load()

    assert "route_reason" not in docs[0].metadata


def test_file_split_yields_one_document(pdf):
    docs = HexReadLoader(pdf, split="file", client=_client(pages=2)).load()

    assert len(docs) == 1
    assert docs[0].page_content == "report.pdf page 1\n\nreport.pdf page 2"
    assert docs[0].metadata["source"] == str(pdf)
    assert docs[0].metadata["total_pages"] == 2
    assert "page" not in docs[0].metadata
    assert "page_label" not in docs[0].metadata


def test_several_inputs_in_one_loader(tmp_path):
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"%PDF-1.7 a")
    second.write_bytes(b"%PDF-1.7 b")

    docs = HexReadLoader([first, second], client=_client(pages=2), max_workers=2).load()

    assert len(docs) == 4
    assert [d.metadata["source"] for d in docs] == [str(first)] * 2 + [str(second)] * 2
    assert [d.page_content for d in docs] == [
        "a.pdf page 1",
        "a.pdf page 2",
        "b.pdf page 1",
        "b.pdf page 2",
    ]


def test_bytes_input_is_one_document(pdf):
    docs = HexReadLoader(b"%PDF-1.7 raw", client=_client(pages=1)).load()

    assert len(docs) == 1
    assert docs[0].metadata["source"] == "document"


def test_lazy_load_sends_nothing_until_consumed(pdf):
    seen: list[httpx.Request] = []
    loader = HexReadLoader(pdf, client=_client(pages=2, record=seen))

    stream = loader.lazy_load()
    assert inspect.isgenerator(stream)
    assert seen == []

    first = next(stream)
    assert len(seen) == 1
    assert first.metadata["page"] == 0
    assert len(list(stream)) == 1


def test_load_equals_list_of_lazy_load(pdf):
    loader = HexReadLoader(pdf, client=_client(pages=2))

    eager = loader.load()
    lazy = list(loader.lazy_load())

    assert [d.page_content for d in eager] == [d.page_content for d in lazy]
    assert [d.metadata for d in eager] == [d.metadata for d in lazy]


def test_alazy_load_matches_lazy_load(pdf):
    expected = HexReadLoader(pdf, client=_client(pages=2)).load()

    async def collect():
        client = AsyncHexRead(api_key="hr_test", base_url=BASE_URL, transport=_mock(pages=2))
        try:
            return [doc async for doc in HexReadLoader(pdf, client=client).alazy_load()]
        finally:
            await client.aclose()

    docs = asyncio.run(collect())

    assert [d.page_content for d in docs] == [d.page_content for d in expected]
    assert [d.metadata for d in docs] == [d.metadata for d in expected]


def test_construction_never_needs_a_key(monkeypatch, pdf):
    monkeypatch.delenv("HEXREAD_API_KEY", raising=False)

    loader = HexReadLoader(pdf)

    assert loader.file_path == pdf


def test_unknown_split_is_rejected(pdf):
    with pytest.raises(ValueError, match="split"):
        HexReadLoader(pdf, split="chunk")


def test_sync_client_rejected_by_the_async_path(pdf):
    loader = HexReadLoader(pdf, client=_client(pages=1))

    async def collect():
        return [doc async for doc in loader.alazy_load()]

    with pytest.raises(TypeError, match="AsyncHexRead"):
        asyncio.run(collect())
