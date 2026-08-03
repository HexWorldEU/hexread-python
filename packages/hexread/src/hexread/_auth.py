"""Credential and base URL resolution, in the same precedence the HexRead CLI uses."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ._errors import MissingAPIKeyError

DEFAULT_BASE_URL = "https://api.hexread.com/v1"
API_KEY_ENV = "HEXREAD_API_KEY"
BASE_URL_ENV = "HEXREAD_BASE_URL"
BASE_URL_ENV_LEGACY = "HEXREAD_API"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_NO_KEY_MESSAGE = (
    "No HexRead API key found. Either set the HEXREAD_API_KEY environment variable, pass "
    "api_key= to HexRead(), or run `hexread login` with the HexRead CLI. API access requires a "
    "paid plan; create a key in your dashboard at https://hexread.com."
)


def credential_path() -> Path:
    """Where the HexRead CLI stores its key. Must stay in step with the CLI's own choice."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hexread" / "credential"


def _key_from_cli_credential() -> str:
    path = credential_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # The file holds the key alone; tolerate a hand-edited "key=value" line rather than sending
    # it as the Authorization header.
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("hr_"):
            _, _, value = line.partition("=")
            line = value.strip().strip('"')
        if line:
            return line
    return ""


def resolve_api_key(api_key: str | None = None) -> str:
    """Return the key to use: argument, then environment, then the CLI's stored credential."""
    if api_key:
        return api_key.strip()
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env
    stored = _key_from_cli_credential()
    if stored:
        return stored
    raise MissingAPIKeyError(_NO_KEY_MESSAGE, code="no_api_key")


def resolve_base_url(base_url: str | None = None) -> str:
    """Return the API base, refusing a plaintext URL that would leak the key on the wire."""
    raw = (
        base_url
        or os.environ.get(BASE_URL_ENV, "")
        or os.environ.get(BASE_URL_ENV_LEGACY, "")
        or DEFAULT_BASE_URL
    ).strip()
    raw = raw.rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"hexread: base URL must be http or https, got {raw!r}")
    if parts.scheme == "http":
        host = (parts.hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"hexread: refusing to send your API key in cleartext to {raw!r}. "
                "Use https, or a loopback host for local development."
            )
    return raw


def origin_of(base_url: str) -> str:
    """Scheme and host of a base URL, for joining the root-relative links in a job handle."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"
