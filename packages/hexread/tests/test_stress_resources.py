"""Resource discipline under abuse: every handle this library opens must come back.

`convert()` opens a file when it is handed a path, and it opens it before anything that can fail.
These tests drive every exit out of that function - success, a refused upload, a stream that dies,
a result that will not decode, a callback that raises - and assert the handle is closed on each
one. The batch paths do the same over enough files that a leak would show up as a descriptor
count, not just a flag on one object.
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import httpx
import pytest
from conftest import (
    JOB_ID,
    PDF_BYTES,
    error_response,
    event_blocks,
    job_response,
    ok_response,
    sse,
    sse_response,
)

from hexread import HexReadError, ResultDeliveryError, ServerError, ValidationError

FD_DIR = "/proc/self/fd"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every file `prepare_upload` opened for reading, in order. Test fixture writes are not it."""
    handles: list = []
    real_open = pathlib.Path.open

    def spy(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if getattr(handle, "mode", "") == "rb":
            handles.append(handle)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", spy)
    return handles


def all_closed(handles: list) -> bool:
    return bool(handles) and all(handle.closed for handle in handles)


# -- the sync client, one exit at a time -------------------------------------------------------


def test_handle_is_closed_after_a_successful_inline_conversion(make_api, make_client, pdf, opened):
    make_client(make_api(convert=ok_response())).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_the_upload_is_refused(make_api, make_client, pdf, opened):
    api = make_api(convert=error_response(400, type="validation_error", code="encrypted_pdf"))
    with pytest.raises(ValidationError):
        make_client(api).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_the_body_is_not_json(make_api, make_client, pdf, opened):
    api = make_api(convert=httpx.Response(200, text="<html>captive portal</html>"))
    with pytest.raises(ServerError):
        make_client(api).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_the_event_stream_dies(make_api, make_client, pdf, opened, sleeps):
    api = make_api(
        convert=job_response(),
        events=lambda request: dying_stream_response(sse("phase", {"phase": "warming"})),
    )
    with pytest.raises(HexReadError):
        make_client(api).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_the_result_fetch_fails(make_api, make_client, pdf, opened):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=error_response(410, type="gone", code="result_consumed"),
    )
    with pytest.raises(HexReadError):
        make_client(api).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_the_result_will_not_decode(make_api, make_client, pdf, opened):
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=httpx.Response(200, text="not json at all"),
    )
    with pytest.raises(ResultDeliveryError):
        make_client(api).convert(pdf)
    assert all_closed(opened)


def test_handle_is_closed_when_a_progress_callback_raises(make_api, make_client, pdf, opened):
    def explode(update):
        raise RuntimeError("callback is broken")

    api = make_api(convert=ok_response())
    with pytest.raises(RuntimeError):
        make_client(api).convert(pdf, on_progress=explode)
    assert all_closed(opened)


def test_handle_is_closed_when_the_arguments_are_rejected_after_opening(
    make_api, make_client, pdf, opened
):
    # prefer= is validated inside the request builder, after the file is already open.
    with pytest.raises(ValueError):
        make_client(make_api(convert=ok_response())).convert(pdf, prefer="whenever")
    assert all_closed(opened)


def test_a_failure_to_close_never_replaces_the_real_error(make_api, make_client, pdf, opened):
    """Cleanup runs in a finally. An OSError there must not hide why the conversion failed."""
    api = make_api(convert=error_response(402, type="quota_error", code="quota_exceeded"))
    client = make_client(api)

    real_open = pathlib.Path.open

    class Stubborn:
        def __init__(self, handle):
            self._handle = handle
            self.read = handle.read
            self.seek = handle.seek
            self.closed = False

        def close(self):
            self._handle.close()
            self.closed = True
            raise OSError("the filesystem went away")

    handle = Stubborn(real_open(pdf, "rb"))
    opened.append(handle)
    with pytest.raises(HexReadError) as caught:
        client.convert(handle, name="report.pdf")
    assert caught.value.code == "quota_exceeded"


# -- an injected file object belongs to the caller ----------------------------------------------


def test_a_file_object_the_caller_opened_is_left_open(make_api, make_client, tmp_path):
    path = tmp_path / "caller.pdf"
    path.write_bytes(PDF_BYTES)
    with path.open("rb") as handle:
        make_client(make_api(convert=ok_response())).convert(handle)
        assert not handle.closed
        assert handle.read() == b""  # read to the end, and still usable


def test_a_file_object_is_left_open_even_when_the_conversion_fails(make_api, make_client, tmp_path):
    path = tmp_path / "caller.pdf"
    path.write_bytes(PDF_BYTES)
    api = make_api(convert=error_response(413, type="payload_too_large", code="file_too_large"))
    with path.open("rb") as handle:
        with pytest.raises(HexReadError):
            make_client(api).convert(handle)
        assert not handle.closed


# -- batches ------------------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 4])
def test_convert_many_closes_every_handle_it_opened(
    make_api, make_client, tmp_path, opened, workers
):
    paths = []
    for i in range(40):
        path = tmp_path / f"doc-{i:03d}.pdf"
        path.write_bytes(PDF_BYTES)
        paths.append(path)

    results = make_client(make_api(convert=ok_response())).convert_many(paths, max_workers=workers)

    assert len(results) == 40
    assert len(opened) == 40
    assert all_closed(opened)


@pytest.mark.parametrize("workers", [1, 4])
def test_convert_many_closes_every_handle_when_the_server_refuses(
    make_api, make_client, tmp_path, opened, workers
):
    paths = []
    for i in range(20):
        path = tmp_path / f"doc-{i:03d}.pdf"
        path.write_bytes(PDF_BYTES)
        paths.append(path)

    api = make_api(convert=error_response(403, type="permission_error", code="forbidden"))
    results = make_client(api).convert_many(paths, max_workers=workers)

    assert all(isinstance(item, HexReadError) for item in results)
    assert len(opened) == 20
    assert all_closed(opened)


@pytest.mark.skipif(not os.path.isdir(FD_DIR), reason="needs /proc/self/fd")
def test_a_large_batch_leaks_no_descriptors(make_api, make_client, tmp_path):
    path = tmp_path / "one.pdf"
    path.write_bytes(PDF_BYTES)
    client = make_client(make_api(convert=ok_response()))

    client.convert(path)  # warm up: the transport and any lazily opened state
    before = len(os.listdir(FD_DIR))
    client.convert_many([path] * 200, max_workers=4)
    after = len(os.listdir(FD_DIR))

    assert after - before <= 2, f"descriptor count grew from {before} to {after}"


# -- the async client ---------------------------------------------------------------------------


async def test_async_handle_is_closed_on_success(make_api, make_async_client, pdf, opened):
    async with make_async_client(make_api(convert=ok_response())) as client:
        await client.convert(pdf)
    assert all_closed(opened)


async def test_async_handle_is_closed_when_the_upload_is_refused(
    make_api, make_async_client, pdf, opened
):
    api = make_api(convert=error_response(400, type="validation_error", code="invalid_request"))
    async with make_async_client(api) as client:
        with pytest.raises(ValidationError):
            await client.convert(pdf)
    assert all_closed(opened)


async def test_async_handle_is_closed_when_the_body_is_not_json(
    make_api, make_async_client, pdf, opened
):
    async with make_async_client(make_api(convert=httpx.Response(202, content=b""))) as client:
        with pytest.raises(ServerError):
            await client.convert(pdf)
    assert all_closed(opened)


async def test_async_handle_is_closed_when_the_conversion_is_cancelled(
    make_api, make_async_client, pdf, opened
):
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("the request should have been cancelled")

    async with make_async_client(make_api(convert=slow)) as client:
        task = asyncio.ensure_future(client.convert(pdf))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all_closed(opened)
        # The client itself survives a cancelled conversion.
        assert not client._http.is_closed


async def test_async_file_object_the_caller_opened_is_left_open(
    make_api, make_async_client, tmp_path
):
    path = tmp_path / "caller.pdf"
    path.write_bytes(PDF_BYTES)
    handle = path.open("rb")
    try:
        async with make_async_client(make_api(convert=ok_response())) as client:
            await client.convert(handle)
        assert not handle.closed
    finally:
        handle.close()


async def test_async_batch_closes_every_handle(make_api, make_async_client, tmp_path, opened):
    paths = []
    for i in range(30):
        path = tmp_path / f"doc-{i:03d}.pdf"
        path.write_bytes(PDF_BYTES)
        paths.append(path)

    async with make_async_client(make_api(convert=ok_response())) as client:
        results = await client.convert_many(paths, max_workers=5)

    assert len(results) == 30
    assert len(opened) == 30
    assert all_closed(opened)


# -- helpers -------------------------------------------------------------------------------------


class DyingStream(httpx.SyncByteStream):
    """Emits a few bytes, then fails the way a dropped connection does."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __iter__(self):
        yield self._text.encode()
        raise httpx.ReadError("connection reset by peer")


def dying_stream_response(text: str) -> httpx.Response:
    return httpx.Response(
        200, stream=DyingStream(text), headers={"content-type": "text/event-stream"}
    )


def test_the_dying_stream_helper_really_dies(make_api, make_client, pdf, sleeps):
    """Guards the helper above: a test that silently stopped failing would prove nothing."""
    api = make_api(
        convert=job_response(job_id=JOB_ID),
        events=lambda request: dying_stream_response(sse("phase", {"phase": "warming"})),
    )
    with pytest.raises(HexReadError) as caught:
        make_client(api).convert(pdf)
    assert caught.value.code == "stream_lost"
    assert len(api.sent("events")) > 1  # it reconnected, so the drop was real
