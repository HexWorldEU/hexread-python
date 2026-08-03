"""Reader tests.

The API is faked with httpx.MockTransport, so the suite never opens a socket and needs no key.
The module is skipped when llama-index-core is not installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("llama_index.core")

from hexread import AsyncHexRead, HexRead, MissingAPIKeyError  # noqa: E402

from llama_index.readers.hexread import HexReadReader  # noqa: E402

PAGES = [
    {"index": 0, "markdown": "# Agreement\n\nSigned in Berlin."},
    {"index": 1, "markdown": "## Fees\n\nEUR 24.95 per month."},
]
RESULT = {
    "markdown": "# Agreement\n\nSigned in Berlin.\n\n## Fees\n\nEUR 24.95 per month.",
    "pages": PAGES,
    "meta": {
        "pages": 2,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb924" * 2,
        "timing_ms": 812,
        "backend": "vlm",
        "model": "mineru-2.5",
        "route_reason": "auto: scanned pages",
    },
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/convert")
    return httpx.Response(200, json=RESULT, headers={"X-HexRead-Quota-Pages-Remaining": "498"})


def _reader(**kwargs: Any) -> HexReadReader:
    client = HexRead(api_key="hr_test", transport=httpx.MockTransport(_handler))
    return HexReadReader(client=client, **kwargs)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "contract.pdf"
    path.write_bytes(b"%PDF-1.7 stand-in bytes")
    return path


def test_page_split_gives_one_document_per_page(pdf: Path) -> None:
    docs = _reader().load_data(pdf)

    assert [doc.text for doc in docs] == [page["markdown"] for page in PAGES]
    first = docs[0].metadata
    assert first["source"] == str(pdf)
    assert first["page"] == 0
    assert first["page_label"] == "1"
    assert first["total_pages"] == 2
    assert first["model"] == "mineru-2.5"
    assert first["route_reason"] == "auto: scanned pages"
    assert first["parser"] == "hexread"
    assert docs[1].metadata["page"] == 1
    assert docs[1].metadata["page_label"] == "2"


def test_lazy_load_yields_the_same_documents(pdf: Path) -> None:
    lazy = list(_reader().lazy_load_data(pdf))

    assert [doc.text for doc in lazy] == [doc.text for doc in _reader().load_data(pdf)]
    assert lazy[0].metadata["page_label"] == "1"


def test_file_split_gives_one_document_for_the_whole_file(pdf: Path) -> None:
    docs = _reader(split="file").load_data(pdf)

    assert len(docs) == 1
    assert docs[0].text == RESULT["markdown"]
    metadata = docs[0].metadata
    assert metadata["source"] == str(pdf)
    assert metadata["total_pages"] == 2
    assert "page" not in metadata
    assert "page_label" not in metadata


def test_extra_info_is_merged_under_our_keys(pdf: Path) -> None:
    # SimpleDirectoryReader passes its file metadata this way, and it must survive.
    extra_info = {
        "file_name": "contract.pdf",
        "creation_date": "2026-07-31",
        "source": "walker guess",
        "page": 99,
    }

    metadata = _reader().load_data(pdf, extra_info=extra_info)[0].metadata

    assert metadata["file_name"] == "contract.pdf"
    assert metadata["creation_date"] == "2026-07-31"
    assert metadata["source"] == str(pdf)
    assert metadata["page"] == 0


def test_extra_metadata_is_added_to_every_document(pdf: Path) -> None:
    docs = _reader(extra_metadata={"tenant": "acme"}).load_data(pdf)

    assert [doc.metadata["tenant"] for doc in docs] == ["acme", "acme"]


def test_document_ids_are_stable_across_loads(pdf: Path) -> None:
    first = _reader().load_data(pdf)
    second = _reader().load_data(pdf)

    assert [doc.id_ for doc in first] == [f"{pdf}:0", f"{pdf}:1"]
    assert [doc.id_ for doc in second] == [doc.id_ for doc in first]
    assert _reader(split="file").load_data(pdf)[0].id_ == str(pdf)


def test_load_data_many_returns_documents_for_every_file(tmp_path: Path) -> None:
    paths = []
    for name in ("a.pdf", "b.pdf"):
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.7 stand-in bytes")
        paths.append(path)

    docs = _reader().load_data_many(paths, max_workers=2)

    assert len(docs) == 4
    assert [doc.metadata["source"] for doc in docs] == [str(paths[0])] * 2 + [str(paths[1])] * 2


def test_aload_data_returns_the_same_documents(pdf: Path) -> None:
    async def load() -> list[Any]:
        transport = httpx.MockTransport(_handler)
        async with AsyncHexRead(api_key="hr_test", transport=transport) as client:
            return await HexReadReader(client=client).aload_data(pdf)

    docs = asyncio.run(load())

    assert [doc.text for doc in docs] == [page["markdown"] for page in PAGES]
    assert docs[0].metadata["page_label"] == "1"


def test_injected_client_must_match_the_call_style(pdf: Path) -> None:
    # Reusing the wrong one would quietly convert against a different base URL.
    with pytest.raises(TypeError, match="aload_data"):
        asyncio.run(_reader().aload_data(pdf))

    transport = httpx.MockTransport(_handler)
    reader = HexReadReader(client=AsyncHexRead(api_key="hr_test", transport=transport))
    with pytest.raises(TypeError, match="load_data"):
        reader.load_data(pdf)


def test_reader_builds_without_an_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # SimpleDirectoryReader constructs every extractor up front, so a reader for one file type
    # must not fail just because no key is set. The key is needed at conversion time only.
    monkeypatch.delenv("HEXREAD_API_KEY", raising=False)
    for var in ("XDG_CONFIG_HOME", "APPDATA", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))

    reader = HexReadReader()

    with pytest.raises(MissingAPIKeyError):
        reader.load_data(tmp_path / "missing.pdf")


def test_unknown_split_is_rejected() -> None:
    with pytest.raises(ValueError, match="split"):
        HexReadReader(split="chapter")
