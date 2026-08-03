"""Server-sent event parsing for the async conversion path.

Payloads arrive as JSON objects whose values are all strings, `{"event":"page","page":"1"}`, so
they are decoded loosely and coerced here rather than parsed into a strict shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class Event:
    """One decoded SSE message."""

    name: str
    """phase, page, completed, or error."""

    data: dict[str, str] = field(default_factory=dict)
    event_id: str = ""

    @property
    def is_terminal(self) -> bool:
        """True when no further events will follow.

        The one-time result token rides on `completed`, and the result cannot be fetched without
        it, so a `completed` carrying no token is deliberately not terminal.
        """
        if self.name == "error":
            return True
        return self.name == "completed" and bool(self.data.get("token"))


# Ranked so we can tell whether an event moved the job forward. Unknown phases rank lowest.
_PHASE_RANK: dict[str, int] = {
    "queued": 1,
    "warming": 2,
    "processing": 3,
    "completed": 4,
    "failed": 4,
}


@dataclass(slots=True)
class JobState:
    """Running state assembled from the event stream."""

    status: str = "queued"
    pages_done: int = 0
    pages_total: int = 0
    token: str = ""
    result_path: str = ""
    error_code: str = ""
    last_event_id: str = ""
    _high_water: tuple[int, int, int] = field(default=(0, 0, 0), init=False, repr=False)

    def __post_init__(self) -> None:
        self._high_water = self._reached()

    def _reached(self) -> tuple[int, int, int]:
        return (_PHASE_RANK.get(self.status, 0), self.pages_done, self.pages_total)

    def apply(self, event: Event) -> bool:
        """Fold an event in. Returns True only when the job moved *forward*.

        Callers reset their reconnect budget on that signal, so it has to be monotonic. A stream
        that replays its buffered prefix on every reconnect sends `phase: warming` after a page
        event, which walks the status backwards; answering "something changed" there would reset
        the budget forever and leave the client reconnecting in a tight loop until the two hour
        deadline. Progress is measured against a high-water mark instead, which cannot oscillate.
        """
        if event.event_id:
            self.last_event_id = event.event_id

        if event.name == "phase":
            self.status = event.data.get("phase") or self.status
        elif event.name == "page":
            self.status = "processing"
            self.pages_done = _as_int(event.data.get("page"), self.pages_done)
            self.pages_total = _as_int(event.data.get("total"), self.pages_total)
        elif event.name == "completed":
            self.status = "completed"
            self.pages_total = _as_int(event.data.get("pages"), self.pages_total)
            self.pages_done = max(self.pages_done, self.pages_total)
            self.token = event.data.get("token") or self.token
            self.result_path = event.data.get("result") or self.result_path
        elif event.name == "error":
            self.status = "failed"
            self.error_code = event.data.get("code") or "conversion_failed"

        reached = self._reached()
        if reached <= self._high_water:
            return False
        self._high_water = reached
        return True


def parse_lines(lines: Iterable[str]) -> Iterator[Event]:
    """Turn raw SSE lines into events.

    Comment lines (the server sends `: keepalive`), multi-line data, `id:` lines used for resume,
    and the blank line that dispatches an event are all handled here.
    """
    name = ""
    event_id = ""
    data_lines: list[str] = []

    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            if data_lines or name:
                event = _build(name, data_lines, event_id)
                if event is not None:
                    yield event
            name, event_id, data_lines = "", "", []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            name = value
        elif field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            event_id = value

    if data_lines or name:
        event = _build(name, data_lines, event_id)
        if event is not None:
            yield event


def _build(name: str, data_lines: list[str], event_id: str) -> Event | None:
    payload = "\n".join(data_lines).strip()
    data: dict[str, str] = {}
    if payload:
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            data = {str(k): "" if v is None else str(v) for k, v in parsed.items()}
        else:
            data = {"raw": payload}
    resolved = name or data.get("event", "")
    if not resolved:
        return None
    return Event(name=resolved, data=data, event_id=event_id)


def _as_int(value: str | None, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback
