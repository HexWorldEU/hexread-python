# hexread Python SDK

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Convert PDFs and images to Markdown with **[HexRead](https://hexread.com)**, page by page.
Three packages live here: the API client, a LlamaIndex reader, and a LangChain loader. All
three are thin clients for `https://api.hexread.com/v1` and never process documents locally.

Python 3.10+. Input formats: PDF, PNG, JPEG, TIFF, WebP.

## Install

| Package | Install | Use it for |
|---|---|---|
| [`hexread`](packages/hexread) | `pip install hexread` | the client: `convert()`, `HexRead`, `AsyncHexRead` |
| [`llama-index-readers-hexread`](packages/llama-index-readers-hexread) | `pip install llama-index-readers-hexread` | a LlamaIndex pipeline |
| [`langchain-hexread`](packages/langchain-hexread) | `pip install langchain-hexread` | a LangChain pipeline |

Both loaders pull in `hexread`. The client's only runtime dependency is `httpx`. All three
packages share one version and release together on a single `v*` tag.

## Quick start

```python
import hexread

print(hexread.convert("report.pdf"))   # "# Quarterly report\n\nRevenue for the period ..."
```

Keep a client to reuse the connection and to reach the pages and the metadata:

```python
from hexread import HexRead

client = HexRead()                # key from the environment or the CLI credential
res = client.convert("report.pdf")

res.markdown                      # the whole document as Markdown
res.pages[0].markdown             # page one; .index is 0-based, .number is 1-based
res.model                         # the parser that ran, e.g. "mineru-2.5"
res.pages_remaining               # pages left in the current allowance window
print(res)                        # str(res) is res.markdown

for out in client.convert_many(["a.pdf", "b.png"], max_workers=2, progress=True):
    print(out)                    # a failed file comes back as its exception, batch continues
```

`AsyncHexRead` mirrors `HexRead` method for method with `await`. Every option, result field and
error class is listed in the [client README](packages/hexread/README.md).

## In a RAG pipeline

Both loaders return **one Document per page**, so a retriever can cite a page. Pass
`split="file"` for one Document per file. Every Document carries `source`, `page`, `page_label`,
`total_pages`, `model`, `parser`, and `route_reason` in its metadata.

```python
from llama_index.readers.hexread import HexReadReader

docs = HexReadReader().load_data("report.pdf")

docs[0].text        # markdown of page one
docs[0].metadata    # {"source": "report.pdf", "page": 0, "page_label": "1", ...}
```

```python
from langchain_hexread import HexReadLoader

docs = HexReadLoader("report.pdf").load()

docs[0].page_content
```

Batch loading, async twins, and whole-directory ingestion through `SimpleDirectoryReader` are in
the [LlamaIndex reader README](packages/llama-index-readers-hexread/README.md) and the
[LangChain loader README](packages/langchain-hexread/README.md).

## Authentication

**API access requires a paid HexRead plan.** The free trial is web only, so a trial account
converting through the API gets a `PermissionError` with code `api_not_available_on_tier`.
Create and revoke keys in your HexRead dashboard.

The client takes the first key it finds:

1. `HexRead(api_key="hr_live_...")`
2. the `HEXREAD_API_KEY` environment variable, the CI-friendly path
3. the credential file `hexread login` wrote, in your OS config directory

`HEXREAD_BASE_URL` overrides the API base URL. This library only reads a credential, never
writes one, and raises `MissingAPIKeyError` before sending anything if no key resolves.

## Errors

Everything raised subclasses `HexReadError` and carries `.code`, `.status`, `.type`,
`.request_id`, and `.retry_after` when the API sent one. The class is keyed on the API's error
type, not on the HTTP status.

```python
try:
    md = hexread.convert("report.pdf")
except hexread.QuotaError as e:
    print(e.message, e.request_id)     # allowance spent; retrying will not help
except hexread.RateLimitError as e:
    print("retry after", e.retry_after)
except hexread.HexReadError as e:
    print("conversion failed:", e)
```

The full list of classes is in the [client README](packages/hexread/README.md#errors).

## How it works

One `convert()` call is the whole protocol. The client uploads the file, and the API either
answers inline or hands back a job; the client follows whichever path it gets, streams progress
while the pages come through, and fetches the result as soon as it is ready. Results are
delivered once and are not retained, so that fetch happens inside the same call. You never poll,
never track a job id, and never ask for a result twice.

## Development

[go-task](https://taskfile.dev) and [uv](https://docs.astral.sh/uv/) drive everything;
`task --list` shows every target.

```sh
task bootstrap   # create each package's virtualenv
task check       # everything CI runs: fmt-check, lint, test
```

The default test run is offline: every request is served in-process by `httpx.MockTransport`, so
no socket is opened and no credential on the machine is read. The smoke tests that talk to
production are marked `live` and stay skipped unless both `HEXREAD_API_KEY` and
`HEXREAD_TEST_DOCS` are set; they convert real pages, billed to that key.

To release, set the same version in all three `pyproject.toml` files and tag `vX.Y.Z`. GitHub
Actions tests the tagged commit and publishes to PyPI.

Security issues go through [SECURITY.md](SECURITY.md), not the public issue tracker.

## License

[MIT](LICENSE), © HexWorld Solutions GmbH. The HexRead service and its documentation live at
[hexread.com](https://hexread.com); this repository holds the open-source Python SDK and its two
RAG loaders only.
