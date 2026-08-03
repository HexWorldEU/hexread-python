# llama-index-readers-hexread

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE)

A [LlamaIndex](https://docs.llamaindex.ai/) reader for **[HexRead](https://hexread.com)**. It
converts PDFs and images to Markdown through the HexRead API and returns LlamaIndex `Document`s.
Nothing is processed locally.

**API access requires a paid HexRead plan.** The free trial is web only, so no API key can be
issued for it. Keys are created and revoked in your HexRead dashboard.

## Install

```sh
pip install llama-index-readers-hexread
```

Python 3.10+. Pulls in [`hexread`](https://pypi.org/project/hexread/) and `llama-index-core`.

## Quick start

```python
from llama_index.readers.hexread import HexReadReader

docs = HexReadReader().load_data("report.pdf")  # one Document per page
```

Set `HEXREAD_API_KEY`, or pass `api_key=` to the reader. The API client is built on the first
conversion, never at construction, so a reader can be created before a key is available.

`file` is a path, raw bytes, or an open binary file.

## Reader options

| Option | Default | Effect |
|---|---|---|
| `api_key` | environment, then CLI credential | key for this reader |
| `model` | `auto` | parser to request; naming one requires a plan that allows it |
| `lang` | unset | OCR language hint, passed through to the parser |
| `split` | `"page"` | `"page"` for one `Document` per page, `"file"` for one per document |
| `base_url` | `https://api.hexread.com/v1` | API base URL |
| `client` | built on first use | an existing `HexRead` or `AsyncHexRead` to convert with |
| `extra_metadata` | `{}` | extra keys merged into every `Document`'s metadata |

A reader holding a `HexRead` serves the sync methods; one holding an `AsyncHexRead` serves
`aload_data`. Using the wrong one raises `TypeError` with the fix in the message.

## Methods

| Method | Returns | Notes |
|---|---|---|
| `load_data(file, extra_info=None)` | `list[Document]` | converts one file |
| `lazy_load_data(file, extra_info=None)` | `Iterator[Document]` | converts when the first Document is pulled |
| `aload_data(file, extra_info=None)` | `list[Document]` | async; needs an `AsyncHexRead` or no injected client |
| `load_data_many(files, *, max_workers=2, extra_info=None, raise_on_error=False)` | `list[Document]` | several files, a few at a time, in input order |

`extra_info` is merged *underneath* the reader's own keys, so a `SimpleDirectoryReader`'s
`file_name` and `creation_date` survive while `source` and `page` stay authoritative.

`load_data_many` keeps the batch alive when one file fails: by default the failure is logged and
that file's Documents are skipped. Pass `raise_on_error=True` to abort instead. Keep `max_workers`
at or below the concurrency your plan allows, or the API answers 429.

```python
reader = HexReadReader()

docs = reader.load_data_many(["a.pdf", "b.pdf", "scan.png"], max_workers=2)
```

Async, with a client the reader does not own and so does not close:

```python
from hexread import AsyncHexRead
from llama_index.readers.hexread import HexReadReader

async with AsyncHexRead() as client:
    docs = await HexReadReader(client=client).aload_data("report.pdf")
```

With no injected client, `aload_data` builds one for the call and closes it before returning.

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

Each `Document` also gets a stable `id_`: `"<source>:<page index>"` per page, or `"<source>"` when
`split="file"`, so re-ingesting a document updates in place instead of duplicating.

## A whole directory

`HexReadReader` works as a `SimpleDirectoryReader` file extractor:

```python
from llama_index.core import SimpleDirectoryReader
from llama_index.readers.hexread import HexReadReader

reader = HexReadReader()
extractor = {ext: reader for ext in (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".webp")}

docs = SimpleDirectoryReader("./contracts", file_extractor=extractor).load_data()
```

## License

Licensed under the [MIT License](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE),
© HexWorld Solutions GmbH.

Source, issues and the core client:
[github.com/HexWorldEU/hexread-python](https://github.com/HexWorldEU/hexread-python).
