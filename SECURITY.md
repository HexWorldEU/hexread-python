# Security Policy

## Reporting a vulnerability

Please report vulnerabilities **privately** - do not open a public issue.

- Preferred: [GitHub private vulnerability reporting](https://github.com/HexWorldEU/hexread-python/security/advisories/new)
  ("Report a vulnerability" on this repository's Security tab).
- Email: security@hexread.com

You'll get an acknowledgement within 72 hours. Please include a reproduction where possible.

## Scope

This repository contains the open-source `hexread` Python SDK and its two RAG loaders
(`llama-index-readers-hexread`, `langchain-hexread`) - all pure API clients. Issues in the
HexRead **service** (api.hexread.com, hexread.com) are also welcome through the same channels.

## API keys

The client reads a key from the `api_key=` argument, `HEXREAD_API_KEY`, or the credential file
the `hexread` CLI wrote; it never writes a credential itself. Keys are revoked in your HexRead
dashboard. Exceptions carry the request id, never the key - but a traceback can still show a
key you passed as a literal, so scrub anything you attach to a report.

## Releases

Every release of all three packages is built and published to PyPI by GitHub Actions in this
repository, from a `v*` tag; nothing is uploaded from a developer machine. Pin versions in your
lock file. If a package on PyPI claims to be a HexRead package but is not built from this
repository, report it through the channels above.
