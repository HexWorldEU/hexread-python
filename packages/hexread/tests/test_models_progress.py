"""Result models and the progress display."""

from __future__ import annotations

import io
import time

import pytest

from hexread import ConvertResult, Page, Progress, Usage
from hexread._progress import ProgressPrinter, resolve_progress


def test_from_payload_tolerates_a_payload_with_nothing_but_markdown():
    result = ConvertResult.from_payload({"markdown": "# Title\n"})

    assert result.markdown == "# Title\n"
    assert result.pages == []
    assert result.page_count == 0
    assert result.model == ""
    assert result.route_reason == ""
    assert result.meta.backend == ""
    assert result.meta.sha256 == ""
    assert result.meta.timing_ms == 0
    assert result.pages_remaining is None
    assert result.job_id == ""
    assert result.source == ""


def test_from_payload_fills_in_missing_optional_meta_fields():
    result = ConvertResult.from_payload(
        {"markdown": "x", "meta": {"pages": 2, "sha256": "ab", "timing_ms": 10, "backend": "vlm"}}
    )

    assert result.page_count == 2
    assert result.model == ""
    assert result.route_reason == ""


def test_from_payload_survives_a_null_meta_and_null_pages():
    result = ConvertResult.from_payload({"markdown": "x", "meta": None, "pages": None})
    assert result.pages == []
    assert result.page_count == 0


def test_from_payload_numbers_pages_by_position_when_the_index_is_missing():
    result = ConvertResult.from_payload(
        {"markdown": "x", "pages": [{"markdown": "one"}, {"markdown": "two"}]}
    )

    assert [page.index for page in result.pages] == [0, 1]
    assert [page.number for page in result.pages] == [1, 2]


def test_from_payload_carries_the_source_quota_and_job_id():
    result = ConvertResult.from_payload(
        {"markdown": "x"}, source="report.pdf", pages_remaining=7, job_id="job_1"
    )

    assert result.source == "report.pdf"
    assert result.pages_remaining == 7
    assert result.job_id == "job_1"
    assert str(result) == "x"


def test_page_number_is_one_based():
    assert Page(index=0, markdown="").number == 1
    assert Page(index=41, markdown="").number == 42


def test_usage_from_payload_defaults_every_missing_section():
    usage = Usage.from_payload({})

    assert usage.tier == ""
    assert (usage.pages_used, usage.pages_remaining, usage.pages_allowance) == (0, 0, 0)
    assert (usage.concurrency_in_use, usage.concurrency_limit) == (0, 0)
    assert usage.api_access is False
    assert usage.available_models == []
    assert usage.period == ""


@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [(0, 0, 0.0), (3, 0, 0.0), (1, -1, 0.0), (0, 4, 0.0), (2, 4, 0.5), (4, 4, 1.0), (9, 4, 1.0)],
)
def test_progress_fraction(done, total, expected):
    assert Progress(phase="processing", pages_done=done, pages_total=total).fraction == expected


def test_the_printer_writes_to_the_stream_it_is_given():
    buffer = io.StringIO()
    printer = ProgressPrinter(buffer, min_interval=0.0)

    printer(Progress(phase="uploading", source="report.pdf"))
    printer(Progress(phase="processing", pages_done=2, pages_total=4, source="report.pdf"))
    printer(Progress(phase="completed", pages_done=4, pages_total=4, source="report.pdf"))
    printer.close()

    written = buffer.getvalue().splitlines()
    assert written == [
        "hexread: report.pdf uploading",
        "hexread: report.pdf converting 2/4 (50%)",
        "hexread: report.pdf done 4/4 (100%)",
    ]


def test_the_printer_never_writes_to_stdout(capsys):
    """A caller piping markdown to a file must not find progress text in it."""
    buffer = io.StringIO()
    printer = ProgressPrinter(buffer, min_interval=0.0)
    printer(Progress(phase="processing", pages_done=1, pages_total=2, source="a.pdf"))
    printer.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert buffer.getvalue() != ""


def test_the_printer_defaults_to_stderr(capsys):
    printer = ProgressPrinter(min_interval=0.0)
    printer(Progress(phase="completed", pages_done=1, pages_total=1, source="a.pdf"))
    printer.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hexread: a.pdf done 1/1 (100%)" in captured.err


def test_the_printer_throttles_chatty_updates_but_never_the_last_one(monkeypatch):
    ticks = iter([100.0, 100.1, 100.2])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    buffer = io.StringIO()
    printer = ProgressPrinter(buffer, min_interval=1.0)

    printer(Progress(phase="queued", source="a.pdf"))
    printer(Progress(phase="processing", pages_done=1, pages_total=2, source="a.pdf"))
    printer(Progress(phase="completed", pages_done=2, pages_total=2, source="a.pdf"))

    assert buffer.getvalue().splitlines() == [
        "hexread: a.pdf queued",
        "hexread: a.pdf done 2/2 (100%)",
    ]


def test_the_printer_falls_back_to_a_generic_name():
    buffer = io.StringIO()
    ProgressPrinter(buffer, min_interval=0.0)(Progress(phase="warming"))
    assert buffer.getvalue() == "hexread: document starting the model\n"


def test_an_unknown_phase_is_printed_as_it_arrived():
    buffer = io.StringIO()
    ProgressPrinter(buffer, min_interval=0.0)(Progress(phase="reticulating", source="a.pdf"))
    assert buffer.getvalue() == "hexread: a.pdf reticulating\n"


def test_resolve_progress_returns_nothing_when_neither_knob_is_set():
    assert resolve_progress(None, None) == (None, None)
    assert resolve_progress(False, None) == (None, None)


def test_resolve_progress_true_builds_a_printer():
    callback, printer = resolve_progress(True, None)
    assert callback is printer
    assert isinstance(printer, ProgressPrinter)


def test_resolve_progress_accepts_a_callable_in_either_argument():
    def sink(update: Progress) -> None:
        return None

    assert resolve_progress(sink, None) == (sink, None)
    assert resolve_progress(None, sink) == (sink, None)


def test_resolve_progress_fans_out_to_the_printer_and_the_callback(capsys):
    seen: list[Progress] = []
    callback, printer = resolve_progress(True, seen.append)
    assert printer is not None

    callback(Progress(phase="completed", pages_done=1, pages_total=1, source="a.pdf"))

    captured = capsys.readouterr()
    assert [update.phase for update in seen] == ["completed"]
    assert "hexread: a.pdf done 1/1 (100%)" in captured.err
    assert captured.out == ""
