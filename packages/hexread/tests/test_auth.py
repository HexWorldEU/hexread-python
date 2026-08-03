"""Credential and base URL resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from hexread import DEFAULT_BASE_URL, AuthenticationError, HexRead, MissingAPIKeyError
from hexread._auth import (
    API_KEY_ENV,
    BASE_URL_ENV,
    BASE_URL_ENV_LEGACY,
    origin_of,
    resolve_api_key,
    resolve_base_url,
)


@pytest.fixture
def credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the CLI credential lookup at a file this test controls."""

    def write(contents: str) -> Path:
        path = tmp_path / "credential"
        path.write_text(contents, encoding="utf-8")
        monkeypatch.setattr("hexread._auth.credential_path", lambda: path)
        return path

    return write


def test_an_explicit_key_beats_the_environment_and_the_credential_file(monkeypatch, credential):
    credential("hr_from_file\n")
    monkeypatch.setenv(API_KEY_ENV, "hr_from_env")
    assert resolve_api_key("hr_explicit") == "hr_explicit"


def test_the_environment_beats_the_credential_file(monkeypatch, credential):
    credential("hr_from_file\n")
    monkeypatch.setenv(API_KEY_ENV, "hr_from_env")
    assert resolve_api_key() == "hr_from_env"


def test_the_credential_file_is_the_last_resort(monkeypatch, credential):
    credential("hr_from_file\n")
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert resolve_api_key() == "hr_from_file"


def test_surrounding_whitespace_is_trimmed(monkeypatch, credential):
    credential("")
    monkeypatch.setenv(API_KEY_ENV, "  hr_padded  ")
    assert resolve_api_key() == "hr_padded"
    assert resolve_api_key("  hr_arg  ") == "hr_arg"


@pytest.mark.parametrize(
    "contents",
    [
        "hr_from_file\n",
        "# written by hexread login\nhr_from_file\n",
        'api_key = "hr_from_file"\n',
    ],
)
def test_the_credential_file_shapes_the_cli_can_write(monkeypatch, credential, contents):
    credential(contents)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert resolve_api_key() == "hr_from_file"


def test_no_key_anywhere_raises_and_names_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setattr("hexread._auth.credential_path", lambda: tmp_path / "absent")

    with pytest.raises(MissingAPIKeyError) as caught:
        resolve_api_key()

    assert API_KEY_ENV in str(caught.value)
    assert "hexread login" in caught.value.message
    assert caught.value.code == "no_api_key"
    assert isinstance(caught.value, AuthenticationError)


def test_the_client_refuses_to_build_without_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setattr("hexread._auth.credential_path", lambda: tmp_path / "absent")
    with pytest.raises(MissingAPIKeyError):
        HexRead()


def test_the_base_url_defaults_to_production(monkeypatch):
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    monkeypatch.delenv(BASE_URL_ENV_LEGACY, raising=False)
    assert resolve_base_url() == DEFAULT_BASE_URL == "https://api.hexread.com/v1"


def test_the_base_url_environment_variable_wins_over_the_default(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "https://api.hexread.test/v1")
    assert resolve_base_url() == "https://api.hexread.test/v1"


def test_the_legacy_base_url_variable_is_still_read(monkeypatch):
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    monkeypatch.setenv(BASE_URL_ENV_LEGACY, "https://legacy.hexread.test/v1")
    assert resolve_base_url() == "https://legacy.hexread.test/v1"


def test_the_current_base_url_variable_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "https://current.hexread.test/v1")
    monkeypatch.setenv(BASE_URL_ENV_LEGACY, "https://legacy.hexread.test/v1")
    assert resolve_base_url() == "https://current.hexread.test/v1"


def test_an_explicit_base_url_wins_over_both(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "https://env.hexread.test/v1")
    assert (
        resolve_base_url("https://explicit.hexread.test/v1") == "https://explicit.hexread.test/v1"
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://api.hexread.com/v1/", "https://api.hexread.com/v1"),
        ("https://api.hexread.com/v1///", "https://api.hexread.com/v1"),
        ("  https://api.hexread.com/v1/  ", "https://api.hexread.com/v1"),
    ],
)
def test_a_trailing_slash_is_stripped(given, expected):
    assert resolve_base_url(given) == expected


def test_plaintext_http_to_a_remote_host_is_refused():
    with pytest.raises(ValueError, match="cleartext"):
        resolve_base_url("http://api.hexread.com/v1")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
        "http://LOCALHOST:8080/v1",
    ],
)
def test_plaintext_http_to_loopback_is_allowed_for_local_development(url):
    assert resolve_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["ftp://api.hexread.com/v1", "api.hexread.com/v1", "file:///tmp"])
def test_a_base_url_that_is_not_http_is_refused(url):
    with pytest.raises(ValueError, match="must be http or https"):
        resolve_base_url(url)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.hexread.com/v1", "https://api.hexread.com"),
        ("https://api.hexread.com/v1/deeper", "https://api.hexread.com"),
        ("http://localhost:8080/v1", "http://localhost:8080"),
    ],
)
def test_origin_of_keeps_only_the_scheme_and_host(base_url, expected):
    assert origin_of(base_url) == expected
