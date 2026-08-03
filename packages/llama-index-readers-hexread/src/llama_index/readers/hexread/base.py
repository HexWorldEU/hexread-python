"""The HexRead reader for LlamaIndex: a ConvertResult mapped onto Documents a pipeline can cite."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO

from llama_index.core.readers.base import BaseReader
from llama_index.core.schema import Document

from hexread import AsyncHexRead, ConvertResult, HexRead, HexReadError

logger = logging.getLogger(__name__)

Source = str | os.PathLike[str] | bytes | BinaryIO


class HexReadReader(BaseReader):
    """Convert PDFs and images to Markdown Documents through the HexRead API.

        docs = HexReadReader().load_data("report.pdf")

    `split="page"` (the default) keeps a retrieved chunk's page number; `split="file"` returns one
    Document per file. Also works as a SimpleDirectoryReader extractor:

        SimpleDirectoryReader("./docs", file_extractor={".pdf": HexReadReader()}).load_data()

    The client is built on the first conversion, never at construction: SimpleDirectoryReader
    builds every extractor up front, so a missing key must not break the other file types.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        lang: str | None = None,
        split: str = "page",
        base_url: str | None = None,
        client: HexRead | AsyncHexRead | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        if split not in ("page", "file"):
            raise ValueError(f"hexread: split must be 'page' or 'file', got {split!r}")
        self._api_key = api_key
        self._model = model
        self._lang = lang
        self._split = split
        self._base_url = base_url
        self._client = client
        self._extra_metadata = dict(extra_metadata or {})

    def load_data(self, file: Source, extra_info: dict[str, Any] | None = None) -> list[Document]:
        """Convert one file and return its Documents.

        `extra_info` is merged underneath this reader's own keys, so `source` and `page` stay
        authoritative while a SimpleDirectoryReader's file_name and creation_date survive.
        """
        return list(self.lazy_load_data(file, extra_info))

    def lazy_load_data(
        self, file: Source, extra_info: dict[str, Any] | None = None
    ) -> Iterator[Document]:
        """Yield the Documents for one file. The conversion runs when the first one is pulled."""
        result = self._sync_client().convert(file, model=self._model, lang=self._lang)
        yield from self._documents(result, _source_label(file, result), extra_info)

    async def aload_data(
        self, file: Source, extra_info: dict[str, Any] | None = None
    ) -> list[Document]:
        """Convert one file without blocking the event loop.

        An injected client must be an AsyncHexRead; it is reused and left open. Without one, a
        client is built for this call and closed before returning.
        """
        client = self._client
        if isinstance(client, HexRead):
            raise TypeError(
                "hexread: this reader holds a HexRead, which only serves load_data(). "
                "Pass an AsyncHexRead to use aload_data()."
            )
        owned = client is None
        if client is None:
            client = AsyncHexRead(api_key=self._api_key, base_url=self._base_url)
        try:
            result = await client.convert(file, model=self._model, lang=self._lang)
        finally:
            if owned:
                await client.aclose()
        return self._documents(result, _source_label(file, result), extra_info)

    def load_data_many(
        self,
        files: Iterable[Source],
        *,
        max_workers: int = 2,
        extra_info: dict[str, Any] | None = None,
        raise_on_error: bool = False,
    ) -> list[Document]:
        """Convert several files a few at a time, returning every Document in input order.

        Keep `max_workers` at or below the plan's concurrency limit. With `raise_on_error=False` a
        failed file is logged and skipped rather than discarding the rest of the batch.
        """
        items = list(files)
        results = self._sync_client().convert_many(
            items,
            max_workers=max_workers,
            raise_on_error=raise_on_error,
            model=self._model,
            lang=self._lang,
        )
        docs: list[Document] = []
        for item, result in zip(items, results, strict=True):
            if isinstance(result, HexReadError):
                logger.warning("hexread: skipping %s: %s", _source_label(item), result)
                continue
            docs.extend(self._documents(result, _source_label(item, result), extra_info))
        return docs

    def _sync_client(self) -> HexRead:
        if isinstance(self._client, AsyncHexRead):
            raise TypeError(
                "hexread: this reader holds an AsyncHexRead, which only serves aload_data(). "
                "Pass a HexRead to use load_data()."
            )
        if self._client is None:
            self._client = HexRead(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def _documents(
        self,
        result: ConvertResult,
        source: str,
        extra_info: dict[str, Any] | None,
    ) -> list[Document]:
        shared: dict[str, Any] = {
            "total_pages": result.page_count or len(result.pages),
            "model": result.model,
        }
        if result.route_reason:
            shared["route_reason"] = result.route_reason
        shared["parser"] = "hexread"
        # Caller and reader metadata sit underneath, so neither can rewrite the page a chunk came
        # from or the file it belongs to.
        below = {**(extra_info or {}), **self._extra_metadata}

        # A result with no page array still carries the whole markdown; emit that rather than none.
        if self._split == "file" or not result.pages:
            return [_document(result.markdown, {**below, "source": source, **shared}, source)]

        return [
            _document(
                page.markdown,
                {
                    **below,
                    "source": source,
                    "page": page.index,
                    "page_label": str(page.number),
                    **shared,
                },
                f"{source}:{page.index}",
            )
            for page in result.pages
        ]


def _document(text: str, metadata: dict[str, Any], doc_id: str) -> Document:
    """One Document with an id derived from its source, so re-ingesting updates in place."""
    doc = Document(text=text, metadata=metadata)
    doc.id_ = doc_id
    return doc


def _source_label(file: Source, result: ConvertResult | None = None) -> str:
    """Where a document came from: the path as given, else the name the client derived."""
    if isinstance(file, (str, os.PathLike)):
        return str(file)
    return (result.source if result else "") or "document"
