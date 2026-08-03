"""Progress reporting, which must never be the reason a conversion fails.

The distinction these tests pin down is deliberate. A callback the caller wrote is their code: if
it raises, the exception propagates unchanged, because swallowing it would hide their bug and hand
them a conversion that half happened. The built-in printer is this library's code and is purely
decorative, so a stderr that is closed, redirected into a closed pipe, or absent entirely has to
degrade into silence rather than take a paid conversion down with it.
"""

from __future__ import annotations

import io

import httpx
import pytest
from conftest import event_blocks, job_response, ok_response, sse_response

from hexread import HexReadError, Progress, QuotaError
from hexread._progress import ProgressPrinter, resolve_progress


class Recorder:
    """A stream that records what was written and can be told to misbehave."""

    def __init__(self, *, tty: bool = False, fail_after: int | None = None) -> None:
        self.chunks: list[str] = []
        self.flushes = 0
        self._tty = tty
        self._fail_after = fail_after

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        if self._fail_after is not None and len(self.chunks) >= self._fail_after:
            raise OSError(32, "Broken pipe")
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.chunks)


# -- a callback the caller wrote ----------------------------------------------------------------


def test_a_raising_callback_propagates_unchanged(make_api, make_client, pdf):
    boom = RuntimeError("my callback is broken")

    def explode(update: Progress) -> None:
        raise boom

    with pytest.raises(RuntimeError) as caught:
        make_client(make_api(convert=ok_response())).convert(pdf, on_progress=explode)

    assert caught.value is boom


def test_a_raising_callback_leaves_the_client_usable(make_api, make_client, pdf):
    def explode_at_the_end(update: Progress) -> None:
        if update.phase == "completed":
            raise RuntimeError("the last update killed it")

    api = make_api(convert=ok_response())
    client = make_client(api)

    with pytest.raises(RuntimeError):
        client.convert(pdf, on_progress=explode_at_the_end)

    assert client.convert(pdf).markdown.startswith("# Quarterly report")
    assert len(api.sent("convert")) == 2


def test_a_callback_that_raises_mid_stream_does_not_fetch_the_result(make_api, make_client, pdf):
    """Failing between the token and the result leaves the job on the server, not half consumed."""

    def explode_on_pages(update: Progress) -> None:
        if update.pages_done:
            raise RuntimeError("no more updates please")

    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=ok_response(),
    )
    with pytest.raises(RuntimeError):
        make_client(api).convert(pdf, on_progress=explode_on_pages)

    assert api.sent("result") == []


def test_a_callback_that_raises_does_not_hide_a_server_error(make_api, make_client, pdf):
    def explode(update: Progress) -> None:
        raise RuntimeError("callback")

    api = make_api(convert=ok_response(status=200))
    with pytest.raises(RuntimeError):
        make_client(api).convert(pdf, on_progress=explode)


def test_a_server_error_is_not_hidden_by_a_working_callback(make_api, make_client, pdf):
    updates: list[Progress] = []
    api = make_api(
        convert=httpx.Response(402, json={"type": "quota_error", "code": "quota_exceeded"})
    )
    with pytest.raises(QuotaError):
        make_client(api).convert(pdf, on_progress=updates.append)

    assert [u.phase for u in updates] == ["uploading"]


async def test_a_raising_callback_propagates_on_the_async_client(make_api, make_async_client, pdf):
    async with make_async_client(make_api(convert=ok_response())) as client:
        with pytest.raises(ZeroDivisionError):
            await client.convert(pdf, on_progress=lambda update: 1 / 0)


def test_both_a_printer_and_a_callback_run(make_api, make_client, pdf, monkeypatch):
    stream = Recorder()
    monkeypatch.setattr("sys.stderr", stream)
    updates: list[Progress] = []

    make_client(make_api(convert=ok_response())).convert(
        pdf, progress=True, on_progress=updates.append
    )

    assert [u.phase for u in updates] == ["uploading", "completed"]
    assert "report.pdf" in stream.text


def test_the_phases_a_job_reports_in_order(make_api, make_client, pdf):
    api = make_api(
        convert=job_response(), events=sse_response(*event_blocks()), result=ok_response()
    )
    updates: list[Progress] = []
    make_client(api).convert(pdf, on_progress=updates.append)

    assert [u.phase for u in updates] == [
        "uploading",
        "queued",
        "warming",
        "processing",
        "processing",
        "completed",
    ]
    assert [u.pages_done for u in updates] == [0, 0, 0, 1, 3, 3]
    assert updates[-1].fraction == 1.0


# -- the printer this library ships -------------------------------------------------------------


def test_a_closed_stream_never_reaches_the_conversion(make_api, make_client, pdf, monkeypatch):
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stderr", closed)

    result = make_client(make_api(convert=ok_response())).convert(pdf, progress=True)
    assert result.markdown.startswith("# Quarterly report")


def test_a_stream_that_raises_never_reaches_the_conversion(make_api, make_client, pdf, monkeypatch):
    monkeypatch.setattr("sys.stderr", Recorder(fail_after=0))

    result = make_client(make_api(convert=ok_response())).convert(pdf, progress=True)
    assert result.markdown.startswith("# Quarterly report")


def test_a_missing_stderr_never_reaches_the_conversion(make_api, make_client, pdf, monkeypatch):
    monkeypatch.setattr("sys.stderr", None)

    result = make_client(make_api(convert=ok_response())).convert(pdf, progress=True)
    assert result.markdown.startswith("# Quarterly report")


def test_a_broken_printer_does_not_replace_the_real_failure(
    make_api, make_client, pdf, monkeypatch
):
    """close() runs in a finally. An exception there would rewrite the caller's diagnosis."""
    monkeypatch.setattr("sys.stderr", Recorder(tty=True, fail_after=1))

    api = make_api(
        convert=httpx.Response(402, json={"type": "quota_error", "code": "quota_exceeded"})
    )
    with pytest.raises(QuotaError):
        make_client(api).convert(pdf, progress=True)


def test_a_stream_that_breaks_part_way_stops_being_written_to():
    stream = Recorder(fail_after=2)
    printer = ProgressPrinter(stream=stream, min_interval=0.0)

    for phase in ("queued", "warming", "processing", "completed"):
        printer(Progress(phase=phase, source="a.pdf"))
    printer.close()

    assert len(stream.chunks) == 2


def test_a_non_tty_gets_whole_lines_and_no_carriage_returns():
    stream = Recorder(tty=False)
    printer = ProgressPrinter(stream=stream, min_interval=0.0)

    printer(Progress(phase="queued", source="a.pdf"))
    printer(Progress(phase="processing", pages_done=2, pages_total=4, source="a.pdf"))
    printer(Progress(phase="completed", pages_done=4, pages_total=4, source="a.pdf"))
    printer.close()

    assert "\r" not in stream.text
    assert stream.text.count("\n") == 3
    assert "hexread: a.pdf converting 2/4 (50%)" in stream.text
    assert stream.text.endswith("hexread: a.pdf done 4/4 (100%)\n")


def test_a_tty_rewrites_one_line_and_pads_over_the_last_one():
    stream = Recorder(tty=True)
    printer = ProgressPrinter(stream=stream, min_interval=0.0)

    printer(Progress(phase="processing", pages_done=9, pages_total=100, source="a.pdf"))
    printer(Progress(phase="completed", pages_done=100, pages_total=100, source="a.pdf"))

    assert stream.chunks[0].startswith("\r")
    assert stream.text.endswith("\n")
    assert "hexread: a.pdf done 100/100 (100%)" in stream.text


def test_a_tty_line_is_closed_off_when_the_conversion_stops_early():
    stream = Recorder(tty=True)
    printer = ProgressPrinter(stream=stream, min_interval=0.0)

    printer(Progress(phase="processing", pages_done=1, pages_total=3, source="a.pdf"))
    printer.close()

    assert stream.text.endswith("\n")


def test_closing_a_printer_that_never_wrote_writes_nothing():
    stream = Recorder(tty=True)
    ProgressPrinter(stream=stream, min_interval=0.0).close()
    assert stream.chunks == []


def test_updates_are_throttled_but_the_last_one_always_lands():
    stream = Recorder(tty=False)
    printer = ProgressPrinter(stream=stream, min_interval=60.0)

    printer(Progress(phase="queued", source="a.pdf"))
    for page in range(50):
        printer(Progress(phase="processing", pages_done=page, pages_total=50, source="a.pdf"))
    printer(Progress(phase="completed", pages_done=50, pages_total=50, source="a.pdf"))

    assert len(stream.chunks) == 2


def test_a_stream_with_no_isatty_or_flush_is_still_written_to():
    class Minimal:
        def __init__(self) -> None:
            self.text = ""

        def write(self, chunk: str) -> None:
            self.text += chunk

    stream = Minimal()
    printer = ProgressPrinter(stream=stream, min_interval=0.0)
    printer(Progress(phase="completed", source="a.pdf"))
    printer.close()

    assert stream.text == "hexread: a.pdf done\n"


@pytest.mark.parametrize(
    "phase", ["queued", "warming", "processing", "completed", "uploading", "", "\x00", "surprise"]
)
def test_any_phase_name_renders(phase):
    stream = Recorder()
    ProgressPrinter(stream=stream, min_interval=0.0)(Progress(phase=phase, source="a.pdf"))
    assert stream.text.startswith("hexread: a.pdf ")


def test_a_source_name_with_no_value_falls_back_to_document():
    stream = Recorder()
    ProgressPrinter(stream=stream, min_interval=0.0)(Progress(phase="queued", source=""))
    assert "document" in stream.text


def test_resolve_progress_returns_nothing_when_nothing_is_asked_for():
    assert resolve_progress(None, None) == (None, None)
    assert resolve_progress(False, None) == (None, None)


def test_resolve_progress_hands_back_a_bare_callback_unwrapped():
    def callback(update: Progress) -> None:
        return None

    resolved, printer = resolve_progress(callback, None)
    assert resolved is callback
    assert printer is None


def test_the_printer_runs_before_the_callback():
    order: list[str] = []
    stream = Recorder()
    printer = ProgressPrinter(stream=stream, min_interval=0.0)
    resolved, _ = resolve_progress(printer, lambda update: order.append("callback"))

    resolved(Progress(phase="queued", source="a.pdf"))
    assert stream.chunks and order == ["callback"]


def test_a_progress_fraction_is_never_out_of_range():
    assert Progress(phase="x", pages_done=9, pages_total=3).fraction == 1.0
    assert Progress(phase="x", pages_done=1, pages_total=0).fraction == 0.0
    assert Progress(phase="x", pages_done=-4, pages_total=8).fraction == -0.5


def test_the_job_path_survives_a_broken_printer(make_api, make_client, pdf, monkeypatch):
    monkeypatch.setattr("sys.stderr", Recorder(tty=True, fail_after=1))
    api = make_api(
        convert=job_response(), events=sse_response(*event_blocks()), result=ok_response()
    )
    result = make_client(api).convert(pdf, progress=True)

    assert result.markdown.startswith("# Quarterly report")


async def test_the_async_job_path_survives_a_broken_printer(
    make_api, make_async_client, pdf, monkeypatch
):
    monkeypatch.setattr("sys.stderr", Recorder(fail_after=1))
    api = make_api(
        convert=job_response(), events=sse_response(*event_blocks()), result=ok_response()
    )
    async with make_async_client(api) as client:
        result = await client.convert(pdf, progress=True)

    assert isinstance(result.markdown, str)


def test_progress_never_goes_to_stdout(make_api, make_client, pdf, capsys):
    make_client(make_api(convert=ok_response())).convert(pdf, progress=True)
    captured = capsys.readouterr()

    assert captured.out == "", "markdown piped to a file must not pick up progress text"
    assert "hexread:" in captured.err


def test_an_error_after_a_progress_update_is_still_the_error_raised(make_api, make_client, pdf):
    updates: list[Progress] = []
    api = make_api(
        convert=job_response(),
        events=sse_response(*event_blocks()),
        result=httpx.Response(410, json={"type": "gone", "code": "result_consumed"}),
    )
    with pytest.raises(HexReadError) as caught:
        make_client(api).convert(pdf, on_progress=updates.append)

    assert caught.value.code == "result_consumed"
    assert updates[-1].phase == "completed"
