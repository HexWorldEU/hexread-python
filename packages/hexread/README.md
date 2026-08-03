# hexread

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE)

`hexread` is the official Python client for **[HexRead](https://hexread.com)** - convert PDFs and
images to Markdown. It is a thin API client: it talks to the HexRead API at
`https://api.hexread.com/v1` and never processes documents locally.

**API access requires a paid HexRead plan.** The free trial is web only, so no API key can be
issued for it. Keys are created and revoked in your HexRead dashboard.

## Install

```sh
pip install hexread
```

Python 3.10+. The only runtime dependency is [httpx](https://www.python-httpx.org/).

## Quick start

```python
import hexread

res = hexread.convert("report.pdf")  # -> ConvertResult
print(res)                           # str(res) is res.markdown
```

Use a client when you want metadata, several files, or a custom timeout:

```python
from hexread import HexRead

client = HexRead()  # key from the argument, the environment, or the CLI credential
res = client.convert("report.pdf", progress=True)

print(res.model, res.route_reason, res.pages_remaining)
for page in res.pages:
    print(page.index, page.markdown[:80])

for res in client.convert_many(["a.pdf", "b.png"], max_workers=2):
    print(res)  # str(res) is res.markdown; a failed file yields the exception

print(client.usage().pages_remaining)
```

`AsyncHexRead` has the same surface with `await`:

```python
import asyncio
from hexread import AsyncHexRead

async def main():
    async with AsyncHexRead() as client:
        res = await client.convert("report.pdf")
        print(res.markdown)

asyncio.run(main())
```

## Options

| Argument | On | Meaning |
|---|---|---|
| `api_key` | `HexRead(...)` | key for this client; wins over the environment and the CLI credential |
| `base_url` | `HexRead(...)` | API base URL (default `https://api.hexread.com/v1`) |
| `timeout` | `HexRead(...)` | request timeout in seconds |
| `max_retries` | `HexRead(...)` | retries for the failures a repeat can fix (default 4) |
| `model` | client and `convert()` | parser to request; `auto` (the default) lets the server route |
| `lang` | client and `convert()` | OCR language hint, passed through to the parser |
| `prefer` | `convert()` | `"sync"` or `"async"`; by default the client decides |
| `name` | `convert()` | the file name to report when you pass bytes or an open file |
| `progress` | `convert()`, `convert_many()` | print a progress line while the conversion runs |
| `on_progress` | `convert()` | callback receiving `Progress(phase, pages_done, pages_total, source)` |
| `max_workers` | `convert_many()` | files converted in parallel (default 2) |
| `raise_on_error` | `convert_many()` | `True` aborts the batch on the first failure instead of returning it in place |

Naming a `model` requires a plan that allows it; `auto` is always allowed.
`client.usage().available_models` lists the ones you may request.

## The result object

`convert()` returns a `ConvertResult`:

| Field | Type | What |
|---|---|---|
| `markdown` | `str` | the whole document as Markdown |
| `pages` | `list[Page]` | one entry per page, in order |
| `pages[i].index` | `int` | zero-based page index, as returned by the API |
| `pages[i].number` | `int` | the 1-based page number |
| `pages[i].markdown` | `str` | that page's Markdown |
| `model` | `str` | the parser that produced this result |
| `route_reason` | `str` | why `auto` picked that parser; empty when you named a model |
| `page_count` | `int` | pages converted |
| `pages_remaining` | `int \| None` | pages left in the current allowance window |
| `source` | `str` | the path or name you passed in |
| `job_id` | `str` | set when the conversion took the async path, otherwise empty |
| `meta` | `Meta` | `pages`, `sha256`, `timing_ms`, `backend`, `model`, `route_reason` |
| `str(result)` | `str` | the same string as `.markdown` |

Account meters come from `client.usage()`:

```python
u = client.usage()

u.tier                # your plan
u.pages_used          # pages converted in the current window
u.pages_remaining     # pages left before the cap
u.pages_allowance     # the window's allowance
u.concurrency_limit   # conversions this plan may run at once
u.api_access          # True on a plan that may call the API
u.available_models    # models you may request explicitly
u.resets_at           # when the window rolls over
```

## Credentials

Resolved in this order; the first one found wins.

| Order | Source |
|---|---|
| 1 | `api_key=` passed to `HexRead()` or `AsyncHexRead()` |
| 2 | `HEXREAD_API_KEY` |
| 3 | the credential file written by `hexread login` (`hexread/credential` in your OS config directory) |

| Environment variable | Meaning |
|---|---|
| `HEXREAD_API_KEY` | API key for this process (nothing is written to disk) |
| `HEXREAD_BASE_URL` | API base URL (default `https://api.hexread.com/v1`; `HEXREAD_API` is a legacy alias) |

## Errors

Every exception subclasses `HexReadError` and carries `.message`, `.status`, `.type`, `.code`,
`.request_id` and `.retry_after`. The class is keyed on the API's error type, not the HTTP status.

| Exception | Raised when |
|---|---|
| `MissingAPIKeyError` | no credential was found in any of the three sources above |
| `AuthenticationError` | the key is malformed, revoked, or rejected |
| `PermissionError` | authenticated but not allowed: plan gate, scope, or a model outside the plan |
| `QuotaError` | the page allowance for the current period is used up |
| `RateLimitError` | too many requests; `.retry_after` holds the wait in seconds |
| `ValidationError` | the request or the file was rejected |
| `NotFoundError` | unknown job or endpoint |
| `ConflictError` | the job is in a conflicting state |
| `GoneError` | the result is gone (results are not retained) |
| `PayloadTooLargeError` | the upload exceeds the size limit |
| `CapacityError` | transient: no capacity right now, retry later |
| `CanceledError` | the job was canceled |
| `ServerError` | the API failed |

`MissingAPIKeyError` subclasses `AuthenticationError`, so one `except` covers both.
`hexread.PermissionError` shadows the builtin inside `from hexread import *`; import the module or
alias it if that matters to you.

One more class covers a rare case: `ResultDeliveryError` means a result was fetched but could not
be handed back (a disk write failed, say). It carries the text on `.markdown`, because a result is
delivered once and the API cannot serve it again.

## Related packages

| Package | Import | Purpose |
|---|---|---|
| [`llama-index-readers-hexread`](https://github.com/HexWorldEU/hexread-python/tree/main/packages/llama-index-readers-hexread) | `llama_index.readers.hexread` | LlamaIndex reader |
| [`langchain-hexread`](https://github.com/HexWorldEU/hexread-python/tree/main/packages/langchain-hexread) | `langchain_hexread` | LangChain document loader |

## License

Licensed under the [MIT License](https://github.com/HexWorldEU/hexread-python/blob/main/LICENSE),
© HexWorld Solutions GmbH.

The HexRead service and its documentation live at [hexread.com](https://hexread.com); this
repository contains the open-source client only.
