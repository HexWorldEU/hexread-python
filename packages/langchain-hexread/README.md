# langchain-hexread

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE)

A [LangChain](https://python.langchain.com/) document loader for
**[HexRead](https://hexread.com)**. It converts PDFs and images to Markdown through the HexRead API
and returns LangChain `Document`s. Nothing is processed locally.

**API access requires a paid HexRead plan.** The free trial is web only, so no API key can be
issued for it. Keys are created and revoked in your HexRead dashboard.

## Install

```sh
pip install langchain-hexread
```

Python 3.10+. Pulls in [`hexread`](https://pypi.org/project/hexread/) and `langchain-core`.

## Quick start

```python
from langchain_hexread import HexReadLoader

docs = HexReadLoader("report.pdf").load()  # one Document per page
```

Set `HEXREAD_API_KEY`, or pass `api_key=` to the loader. Nothing is resolved until a load starts,
so building a loader never fails for a missing key.

`file_path` is a path, raw bytes, or an open binary file, **or a sequence of them**. A sequence is
converted with bounded concurrency and the Documents come back in input order:

```python
docs = HexReadLoader(["a.pdf", "b.pdf", "scan.png"], max_workers=2).load()
```

Keep `max_workers` at or below the concurrency your plan allows, or the API answers 429.

## Loader options

| Option | Default | Effect |
|---|---|---|
| `api_key` | environment, then CLI credential | key for this loader |
| `model` | `auto` | parser to request; naming one requires a plan that allows it |
| `lang` | unset | OCR language hint, passed through to the parser |
| `split` | `"page"` | `"page"` for one `Document` per page, `"file"` for one per document |
| `base_url` | `https://api.hexread.com/v1` | API base URL |
| `client` | built per load | an existing `HexRead` (sync) or `AsyncHexRead` (async) to convert with |
| `extra_metadata` | `{}` | extra keys merged into every `Document`'s metadata |
| `max_workers` | `2` | files converted in parallel when `file_path` is a sequence |

A loader builds and closes its own client unless you inject one, in which case it is left open for
you to reuse and close. Handing `load()` an `AsyncHexRead` (or `aload()` a `HexRead`) raises
`TypeError` with the fix in the message.

## Methods

The four `BaseLoader` entry points all work:

| Method | Returns | Notes |
|---|---|---|
| `load()` | `list[Document]` | everything at once |
| `lazy_load()` | `Iterator[Document]` | nothing is uploaded until the iterator is consumed |
| `aload()` | `list[Document]` | async, driven by `AsyncHexRead` |
| `alazy_load()` | `AsyncIterator[Document]` | the async twin of `lazy_load` |

`lazy_load()` hands each page over as it is built, so a splitter or a vector store can start
before the whole list exists:

```python
for doc in HexReadLoader("report.pdf").lazy_load():
    index(doc)  # your splitter, embedder, or store
```

## Document metadata

| Key | Value |
|---|---|
| `source` | the path that was converted |
| `page` | page index, 0-based (absent when `split="file"`) |
| `page_label` | page number as a string, 1-based (absent when `split="file"`) |
| `total_pages` | pages in the converted document |
| `model` | parser that produced the Markdown |
| `route_reason` | why the auto router picked that parser (absent when a model was requested) |
| `parser` | always `hexread` |

`source` and a zero-based `page` are the same shape LangChain's own PDF loaders produce, so
existing chains and citation code keep working.

## License

Licensed under the [MIT License](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE),
© HexWorld Solutions GmbH.

Source, issues and the core client:
[github.com/HexWorldEU/hexread-python](https://github.com/HexWorldEU/hexread-python).
