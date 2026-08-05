"""Dependency-free progress display.

Always stderr, never stdout: a caller piping markdown to a file must not find progress text in it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO

from ._models import Progress

ProgressCallback = Callable[[Progress], None]

_PHASE_LABELS = {
    "uploading": "uploading",
    "queued": "queued",
    "warming": "starting the model",
    "processing": "converting",
    "completed": "done",
}


class ProgressPrinter:
    """Renders Progress updates for a human: one rewritten line on a TTY, plain lines elsewhere.

    Every write is best effort. Stderr is not guaranteed to exist or to work: a daemonised process
    has it closed, `2>&-` makes it raise on write, a broken pipe raises EPIPE, and pythonw sets
    sys.stderr to None. Progress is decoration, so none of that may take down a conversion the
    caller is being billed for, and none of it may replace a real exception on the way out of the
    `finally` that closes this printer.
    """

    def __init__(self, stream: TextIO | None = None, min_interval: float = 0.4) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._min_interval = min_interval
        self._broken = False
        self._tty = self._detect_tty()
        # None until the first write. A 0.0 sentinel would be compared against time.monotonic(),
        # whose origin is arbitrary, and on a freshly booted machine that swallows the first update.
        self._last_write: float | None = None
        self._width = 0
        self._finished = False

    def __call__(self, progress: Progress) -> None:
        now = time.monotonic()
        final = progress.phase == "completed"
        throttled = self._last_write is not None and now - self._last_write < self._min_interval
        if throttled and not final:
            return
        self._last_write = now

        line = self._render(progress)
        if self._tty:
            pad = max(0, self._width - len(line))
            self._write("\r" + line + " " * pad)
            self._width = len(line)
            if final:
                self._write("\n")
                self._finished = True
        else:
            self._write(line + "\n")
        self._flush()

    def close(self) -> None:
        """End the line if a conversion stopped before reaching the completed phase."""
        if self._tty and self._width and not self._finished:
            self._write("\n")
            self._flush()
        self._finished = True

    def _render(self, progress: Progress) -> str:
        label = _PHASE_LABELS.get(progress.phase, progress.phase)
        name = progress.source or "document"
        if progress.pages_total > 0:
            pct = int(progress.fraction * 100)
            return f"hexread: {name} {label} {progress.pages_done}/{progress.pages_total} ({pct}%)"
        return f"hexread: {name} {label}"

    def _detect_tty(self) -> bool:
        # A closed stream raises here; either way it is not a terminal.
        try:
            return bool(self._stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def _write(self, text: str) -> None:
        if self._broken:
            return
        try:
            self._stream.write(text)
        except (AttributeError, OSError, ValueError):
            self._broken = True

    def _flush(self) -> None:
        if self._broken:
            return
        try:
            self._stream.flush()
        except (AttributeError, OSError, ValueError):
            self._broken = True


def resolve_progress(
    progress: bool | ProgressCallback | None,
    on_progress: ProgressCallback | None,
) -> tuple[ProgressCallback | None, ProgressPrinter | None]:
    """Turn the two user-facing knobs into one callback.

    `progress=True` prints, a callable receives updates, and both may be set at once.
    """
    printer: ProgressPrinter | None = None
    callbacks: list[ProgressCallback] = []

    if progress is True:
        printer = ProgressPrinter()
        callbacks.append(printer)
    elif callable(progress):
        callbacks.append(progress)

    if on_progress is not None:
        callbacks.append(on_progress)

    if not callbacks:
        return None, None
    if len(callbacks) == 1:
        return callbacks[0], printer

    def fan_out(update: Progress) -> None:
        for callback in callbacks:
            callback(update)

    return fan_out, printer
