"""What callers actually pass in, including the things they should not.

Two separate concerns live here. Going out: a source can be a path, bytes, a handle, a symlink, a
file that vanishes, or something that is none of those, and the name attached to it can contain
anything a filesystem allows. Coming back: markdown is arbitrary text, and this client must hand
it over byte for byte rather than normalising it into something an index will not match.
"""

from __future__ import annotations

import io
import os
import sys

import httpx
import pytest
from conftest import PDF_BYTES, convert_payload, ok_response

from hexread import HexReadError, ResultDeliveryError
from hexread._transport import prepare_upload

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


def file_part_of(api):
    """The uploaded part of the single convert request the fake served."""
    from conftest import file_part

    return file_part(api.only("convert"))


def assert_one_clean_part_header(request: httpx.Request) -> None:
    """The file part must be exactly boundary, disposition, type - and nothing a name smuggled in.

    A raw CR or LF inside a filename would end the Content-Disposition line early and let the rest
    of the name become a header of its own, which is header injection into the upload.
    """
    head = request.content.split(b"\r\n\r\n", 1)[0]
    lines = head.split(b"\r\n")

    assert len(lines) == 3, f"a name opened extra header lines: {lines}"
    assert lines[1].startswith(b"Content-Disposition: form-data;")
    assert lines[2].startswith(b"Content-Type:")
    stripped = head.replace(b"\r\n", b"")
    assert b"\r" not in stripped and b"\n" not in stripped


# -- sources -----------------------------------------------------------------------------------


def test_empty_bytes_are_uploaded_as_an_empty_file(make_api, make_client):
    api = make_api(convert=ok_response())
    make_client(api).convert(b"")

    part = file_part_of(api)
    assert part.value == b""
    assert part.filename == "document"


def test_a_zero_byte_file_is_uploaded(make_api, make_client, tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    api = make_api(convert=ok_response())
    make_client(api).convert(path)

    assert file_part_of(api).value == b""
    assert file_part_of(api).filename == "empty.pdf"


@pytest.mark.parametrize("wrapper", [bytes, bytearray, memoryview])
def test_every_bytes_like_source_is_accepted(make_api, make_client, wrapper):
    api = make_api(convert=ok_response())
    make_client(api).convert(wrapper(PDF_BYTES))
    assert file_part_of(api).value == PDF_BYTES


def test_a_four_megabyte_payload_arrives_whole(make_api, make_client):
    payload = b"%PDF-1.7\n" + os.urandom(4 * 1024 * 1024)
    api = make_api(convert=ok_response())
    make_client(api).convert(payload)
    assert file_part_of(api).value == payload


@pytest.mark.slow
def test_a_sixty_four_megabyte_payload_arrives_whole(make_api, make_client):
    payload = b"%PDF-1.7\n" + b"\xa5" * (64 * 1024 * 1024)
    api = make_api(convert=ok_response())
    make_client(api).convert(payload)
    assert len(file_part_of(api).value) == len(payload)


def test_a_symlink_is_followed(make_api, make_client, tmp_path):
    target = tmp_path / "real.pdf"
    target.write_bytes(PDF_BYTES)
    link = tmp_path / "link.pdf"
    link.symlink_to(target)

    api = make_api(convert=ok_response())
    result = make_client(api).convert(link)

    assert file_part_of(api).value == PDF_BYTES
    assert file_part_of(api).filename == "link.pdf"
    assert result.source == "link.pdf"


def test_a_broken_symlink_is_reported_as_a_missing_file(make_api, make_client, tmp_path):
    link = tmp_path / "dangling.pdf"
    link.symlink_to(tmp_path / "was-never-there.pdf")

    with pytest.raises(FileNotFoundError):
        make_client(make_api(convert=ok_response())).convert(link)


def test_a_file_that_disappears_after_the_check_still_raises_file_not_found(
    make_api, make_client, tmp_path, monkeypatch
):
    """The check and the open cannot be atomic. The failure must still be the obvious one."""
    path = tmp_path / "vanishing.pdf"
    path.write_bytes(PDF_BYTES)

    real_exists = type(path).exists

    def exists_then_delete(self, *args, **kwargs):
        answer = real_exists(self, *args, **kwargs)
        if self == path and answer:
            os.unlink(self)
        return answer

    monkeypatch.setattr(type(path), "exists", exists_then_delete)
    with pytest.raises(FileNotFoundError):
        make_client(make_api(convert=ok_response())).convert(path)


def test_a_directory_is_refused_with_advice(make_api, make_client, tmp_path):
    with pytest.raises(IsADirectoryError) as caught:
        make_client(make_api(convert=ok_response())).convert(tmp_path)
    assert "convert_many" in str(caught.value)


@pytest.mark.parametrize(
    "source",
    [None, 7, 3.5, True, [], {}, set(), object(), Ellipsis, (b"a", b"b")],
    ids=lambda s: type(s).__name__,
)
def test_a_source_that_is_not_a_document_is_refused(source):
    with pytest.raises(TypeError) as caught:
        prepare_upload(source)
    assert "hexread:" in str(caught.value)


def test_an_object_with_a_read_attribute_that_is_not_a_file_is_still_taken(make_api, make_client):
    """Duck typing has a limit: we accept it, and httpx is what refuses it."""

    class Pretender:
        read = "not callable"

    with pytest.raises(Exception) as caught:  # noqa: B017 - httpx picks the type, not us
        make_client(make_api(convert=ok_response())).convert(Pretender())
    assert not isinstance(caught.value, HexReadError)


# -- names -------------------------------------------------------------------------------------

HOSTILE_NAMES = [
    "",
    " ",
    "report.pdf",
    "ünïcodé-Ünïcodé.pdf",
    "\U0001f4c4 emoji \U0001f600.pdf",
    "中文文件名.pdf",
    "\u0645\u0644\u0641.pdf",
    "\u202ecod.exe.pdf",
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "with\nnewline.pdf",
    "with\r\ncrlf.pdf",
    'with"quote.pdf',
    "with;semicolon.pdf",
    "x" * 300 + ".pdf",
    "\t\ttabs.pdf",
    "%00.pdf",
    "name with spaces.pdf",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES, ids=range(len(HOSTILE_NAMES)))
def test_any_name_produces_a_well_formed_multipart_body(make_api, make_client, name):
    api = make_api(convert=ok_response())
    make_client(api).convert(PDF_BYTES, name=name)

    assert_one_clean_part_header(api.only("convert"))
    assert file_part_of(api).value == PDF_BYTES


def test_an_empty_name_falls_back_to_the_file_name(make_api, make_client, pdf):
    api = make_api(convert=ok_response())
    result = make_client(api).convert(pdf, name="")

    assert file_part_of(api).filename == "report.pdf"
    assert result.source == "report.pdf"


def test_an_empty_name_on_bytes_falls_back_to_document(make_api, make_client):
    api = make_api(convert=ok_response())
    assert make_client(api).convert(PDF_BYTES, name="").source == "document"


def test_a_file_object_keeps_only_the_base_name(make_api, make_client, tmp_path):
    path = tmp_path / "nested.pdf"
    path.write_bytes(PDF_BYTES)
    api = make_api(convert=ok_response())
    with path.open("rb") as handle:
        result = make_client(api).convert(handle)

    assert result.source == "nested.pdf"
    assert "/" not in (file_part_of(api).filename or "")


def test_a_file_object_with_no_name_is_called_document(make_api, make_client):
    api = make_api(convert=ok_response())
    assert make_client(api).convert(io.BytesIO(PDF_BYTES)).source == "document"


def test_a_file_object_whose_name_is_a_descriptor_number_does_not_crash(make_api, make_client, pdf):
    fd = os.open(pdf, os.O_RDONLY)
    handle = os.fdopen(fd, "rb")
    try:
        api = make_api(convert=ok_response())
        assert make_client(api).convert(handle).source == str(fd)
    finally:
        handle.close()


# -- markdown coming back ------------------------------------------------------------------------

HOSTILE_MARKDOWN = {
    "emoji": "# \U0001f4c4 Report \U0001f600\n\nDone \u2705\n",
    "cjk": "# 四半期報告\n\n売上は12パーセント増加しました。\n",
    "rtl": "# تقرير ربع سنوي\n\nارتفعت الإيرادات بنسبة 12 في المائة.\n",
    "bidi override": "start \u202e reversed \u202c end",
    "nul": "before\x00after",
    "control bytes": "".join(chr(i) for i in range(1, 32)),
    "zero width": "a\u200bb\u200cc\ufeffd",
    "combining marks": "e\u0301\u0301\u0301\u0301 accented",
    "one very long line": "x" * 500_000,
    "many short lines": "line\n" * 50_000,
    "windows newlines": "a\r\nb\r\nc\r\n",
    "lone cr": "a\rb\rc",
    "tabs and trailing space": "col\tcol   \n",
    "html": "<script>alert(1)</script>",
    "backslashes": "C:\\Users\\x\\report.pdf \\n \\u0041",
    "json inside markdown": '{"markdown": "not really"}',
    "astral plane": "\U0001d11e \U0002a6b2 \U0010ffff",
    "empty": "",
}


@pytest.mark.parametrize("markdown", HOSTILE_MARKDOWN.values(), ids=list(HOSTILE_MARKDOWN))
def test_markdown_comes_back_exactly_as_sent(make_api, make_client, markdown):
    payload = convert_payload(
        markdown, pages=[{"index": 0, "markdown": markdown}], meta={"pages": 1}
    )
    api = make_api(convert=httpx.Response(200, json=payload))
    result = make_client(api).convert(b"%PDF-1.7")

    assert result.markdown == markdown
    assert result.pages[0].markdown == markdown
    assert str(result) == markdown


@pytest.mark.parametrize("markdown", HOSTILE_MARKDOWN.values(), ids=list(HOSTILE_MARKDOWN))
async def test_the_async_client_returns_the_same_bytes(make_api, make_async_client, markdown):
    payload = convert_payload(
        markdown, pages=[{"index": 0, "markdown": markdown}], meta={"pages": 1}
    )
    async with make_async_client(make_api(convert=httpx.Response(200, json=payload))) as client:
        assert (await client.convert(b"%PDF-1.7")).markdown == markdown


def test_a_lone_surrogate_survives_the_ascii_escaped_wire_form(make_api, make_client):
    """json.dumps escapes it rather than encoding it, so it can reach a caller. Do not mangle it."""
    body = '{"markdown": "before \\ud800 after", "pages": [], "meta": {"pages": 1}}'
    api = make_api(convert=httpx.Response(200, content=body.encode("ascii")))
    result = make_client(api).convert(b"%PDF-1.7")

    assert result.markdown == "before \ud800 after"


def test_metadata_that_is_not_a_number_keeps_the_markdown(make_api, make_client):
    """An inline body is never stored either. A junk meta field must not throw the document away."""
    payload = convert_payload(meta={"pages": "lots", "sha256": None, "timing_ms": 1})
    api = make_api(convert=httpx.Response(200, json=payload))

    with pytest.raises(ResultDeliveryError) as caught:
        make_client(api).convert(b"%PDF-1.7")

    assert "Quarterly report" in caught.value.markdown
    assert caught.value.job_id == ""


def test_a_page_count_of_zero_gives_a_fraction_of_zero(make_api, make_client):
    payload = convert_payload("", pages=[], meta={"pages": 0, "timing_ms": 0})
    result = make_client(make_api(convert=httpx.Response(200, json=payload))).convert(b"%PDF-1.7")

    assert result.page_count == 0
    assert result.pages == []
    assert result.markdown == ""


@pytest.mark.skipif(sys.platform == "win32", reason="posix file names")
def test_a_file_name_with_a_newline_on_disk_is_uploaded(make_api, make_client, tmp_path):
    path = tmp_path / "odd\nname.pdf"
    path.write_bytes(PDF_BYTES)
    api = make_api(convert=ok_response())
    make_client(api).convert(path)

    assert_one_clean_part_header(api.only("convert"))
