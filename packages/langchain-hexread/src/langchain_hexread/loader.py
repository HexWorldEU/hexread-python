"""The LangChain document loader: turning a HexRead result into Documents with metadata."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from hexread import AsyncHexRead, ConvertResult, HexRead, Page
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

Source = str | Path | bytes | BinaryIO
"""One document to convert: a path, raw bytes, or an open binary file."""

Split = Literal["page", "file"]


class HexReadLoader(BaseLoader):
    """Load PDFs and images as LangChain Documents, converted to Markdown by HexRead.

        docs = HexReadLoader("report.pdf").load()

    One Document per page, carrying `source` and a zero-based `page`, the same shape LangChain's
    own PDF loaders produce; `split="file"` gives a single Document instead. `file_path` may be a
    sequence, converted `max_workers` at a time: keep that at or below your plan's concurrency.
    """

    def __init__(
        self,
        file_path: Source | Sequence[Source],
        api_key: str | None = None,
        *,
        model: str | None = None,
        lang: str | None = None,
        split: Split = "page",
        base_url: str | None = None,
        client: HexRead | AsyncHexRead | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
        max_workers: int = 2,
    ) -> None:
        if split not in ("page", "file"):
            raise ValueError("langchain-hexread: split must be 'page' or 'file'")
        self.file_path = file_path
        self._sources = _as_sources(file_path)
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._lang = lang
        self._split = split
        self._client = client
        self._extra_metadata = dict(extra_metadata or {})
        self._max_workers = max_workers

    def lazy_load(self) -> Iterator[Document]:
        """Yield one Document per page. Nothing is uploaded until the iterator is consumed."""
        client, owned = self._sync_client()
        try:
            results = self._convert_all(client)
            for source, result in zip(self._sources, results, strict=True):
                yield from self._documents(source, result)
        finally:
            if owned:
                client.close()

    async def alazy_load(self) -> AsyncIterator[Document]:
        """The async twin of `lazy_load`, driven by `AsyncHexRead`."""
        client, owned = self._async_client()
        try:
            results = await self._aconvert_all(client)
            for source, result in zip(self._sources, results, strict=True):
                for document in self._documents(source, result):
                    yield document
        finally:
            if owned:
                await client.aclose()

    def _sync_client(self) -> tuple[HexRead, bool]:
        """The client to convert with, and whether this loader owns it and must close it."""
        if self._client is None:
            return HexRead(api_key=self._api_key, base_url=self._base_url), True
        if isinstance(self._client, HexRead):
            return self._client, False
        raise TypeError(
            "langchain-hexread: load() needs a HexRead client but was given an AsyncHexRead. "
            "Use aload() or alazy_load(), or pass client=HexRead(...)."
        )

    def _async_client(self) -> tuple[AsyncHexRead, bool]:
        if self._client is None:
            return AsyncHexRead(api_key=self._api_key, base_url=self._base_url), True
        if isinstance(self._client, AsyncHexRead):
            return self._client, False
        raise TypeError(
            "langchain-hexread: aload() needs an AsyncHexRead client but was given a HexRead. "
            "Use load() or lazy_load(), or pass client=AsyncHexRead(...)."
        )

    def _convert_all(self, client: HexRead) -> list[ConvertResult]:
        if len(self._sources) == 1:
            return [client.convert(self._sources[0], model=self._model, lang=self._lang)]
        results = client.convert_many(
            self._sources,
            max_workers=self._max_workers,
            raise_on_error=True,
            model=self._model,
            lang=self._lang,
        )
        # raise_on_error=True, so every item is a result and never an error.
        return cast(list[ConvertResult], results)

    async def _aconvert_all(self, client: AsyncHexRead) -> list[ConvertResult]:
        if len(self._sources) == 1:
            return [await client.convert(self._sources[0], model=self._model, lang=self._lang)]
        results = await client.convert_many(
            self._sources,
            max_workers=self._max_workers,
            raise_on_error=True,
            model=self._model,
            lang=self._lang,
        )
        return cast(list[ConvertResult], results)

    def _documents(self, source: Any, result: ConvertResult) -> Iterator[Document]:
        label = _label(source, result)
        total = result.page_count or len(result.pages)
        if self._split == "file":
            yield Document(
                page_content=result.markdown,
                metadata=self._metadata(label, result, total),
            )
            return
        # The fallback keeps a result that arrives with markdown but no page array from yielding
        # zero Documents.
        pages = result.pages or [Page(index=0, markdown=result.markdown)]
        for page in pages:
            yield Document(
                page_content=page.markdown,
                metadata=self._metadata(label, result, total, page),
            )

    def _metadata(
        self,
        source: str,
        result: ConvertResult,
        total: int,
        page: Page | None = None,
    ) -> dict[str, Any]:
        """Metadata for one Document. `page` is zero-based, matching LangChain's PDF loaders."""
        metadata: dict[str, Any] = {"source": source}
        if page is not None:
            metadata["page"] = page.index
            metadata["page_label"] = str(page.index + 1)
        metadata["total_pages"] = total
        metadata["model"] = result.model
        if result.route_reason:
            metadata["route_reason"] = result.route_reason
        metadata["parser"] = "hexread"
        metadata.update(self._extra_metadata)
        return metadata


def _as_sources(file_path: Any) -> list[Any]:
    """Normalise the constructor argument to a list of inputs.

    A str, bytes, or open file is one document even though each is iterable, so those match first.
    """
    if isinstance(file_path, (str, os.PathLike, bytes, bytearray, memoryview)):
        return [file_path]
    if hasattr(file_path, "read"):
        return [file_path]
    if hasattr(file_path, "__iter__"):
        return list(file_path)
    return [file_path]


def _label(source: Any, result: ConvertResult) -> str:
    """The `source` metadata value: the path as the caller wrote it, else the uploaded name."""
    if isinstance(source, (str, os.PathLike)):
        return str(source)
    return result.source or "document"
