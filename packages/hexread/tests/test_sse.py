"""The SSE parser and the job state it feeds, tested on raw lines."""

from __future__ import annotations

import pytest

from hexread._sse import Event, JobState, parse_lines


def lines(text: str) -> list[str]:
    """Split a stream the way httpx does: line endings gone, blank separators kept."""
    return text.splitlines()


def test_a_single_event_is_parsed():
    events = list(parse_lines(lines('event: phase\ndata: {"event":"phase","phase":"warming"}\n\n')))

    assert len(events) == 1
    assert events[0].name == "phase"
    assert events[0].data == {"event": "phase", "phase": "warming"}


def test_keepalive_comments_are_ignored():
    stream = (
        ": keepalive\n\n"
        ": keepalive\n\n"
        'event: page\ndata: {"event":"page","page":"2","total":"9"}\n\n'
        ": keepalive\n\n"
    )
    events = list(parse_lines(lines(stream)))

    assert [event.name for event in events] == ["page"]
    assert events[0].data["page"] == "2"


def test_multi_line_data_is_joined_before_decoding():
    stream = 'event: completed\ndata: {"token": "tok_1",\ndata:  "pages": "4"}\n\n'
    events = list(parse_lines(lines(stream)))

    assert len(events) == 1
    assert events[0].data == {"token": "tok_1", "pages": "4"}


def test_the_event_id_is_captured_for_resume():
    stream = 'id: 17\nevent: page\ndata: {"page":"3"}\n\n'
    (event,) = parse_lines(lines(stream))

    assert event.event_id == "17"

    state = JobState()
    state.apply(event)
    assert state.last_event_id == "17"


def test_carriage_returns_are_stripped():
    stream = 'event: phase\r\ndata: {"phase":"queued"}\r\n\r\n'
    (event,) = parse_lines(lines(stream))

    assert event.name == "phase"
    assert event.data == {"phase": "queued"}


def test_a_final_block_without_a_trailing_blank_line_is_still_dispatched():
    (event,) = parse_lines(lines('event: completed\ndata: {"token":"tok_1"}'))
    assert event.name == "completed"


def test_the_event_name_can_come_from_the_payload():
    (event,) = parse_lines(lines('data: {"event":"error","code":"conversion_failed"}\n\n'))
    assert event.name == "error"


def test_a_block_with_neither_a_name_nor_a_payload_name_is_dropped():
    assert list(parse_lines(lines('data: {"page":"1"}\n\n'))) == []


def test_a_payload_that_is_not_json_is_kept_as_raw_text():
    (event,) = parse_lines(lines("event: note\ndata: not json at all\n\n"))
    assert event.data == {"raw": "not json at all"}


def test_every_payload_value_is_coerced_to_a_string():
    """The server sends strings; a numeric or null value must not break the parse."""
    (event,) = parse_lines(lines('event: page\ndata: {"page":1,"total":3,"note":null}\n\n'))
    assert event.data == {"page": "1", "total": "3", "note": ""}


def test_string_page_counts_become_integers_on_the_state():
    state = JobState()
    state.apply(Event(name="page", data={"page": "1", "total": "3"}))

    assert (state.pages_done, state.pages_total) == (1, 3)
    assert state.status == "processing"


def test_a_page_count_that_is_not_a_number_leaves_the_state_alone():
    state = JobState(pages_done=2, pages_total=5)
    state.apply(Event(name="page", data={"page": "many", "total": ""}))

    assert (state.pages_done, state.pages_total) == (2, 5)


def test_apply_reports_movement_only_when_the_phase_or_page_counts_change():
    state = JobState()

    assert state.apply(Event(name="phase", data={"phase": "warming"})) is True
    assert state.apply(Event(name="phase", data={"phase": "warming"})) is False
    assert state.apply(Event(name="page", data={"page": "1", "total": "3"})) is True
    assert state.apply(Event(name="page", data={"page": "1", "total": "3"})) is False
    assert state.apply(Event(name="page", data={"page": "2", "total": "3"})) is True


def test_a_second_completed_event_only_moves_the_state_once():
    state = JobState()
    completed = Event(name="completed", data={"pages": "3", "token": "tok_1"})

    assert state.apply(completed) is True
    assert state.apply(completed) is False
    assert state.token == "tok_1"
    assert (state.pages_done, state.pages_total) == (3, 3)


def test_an_event_id_alone_does_not_count_as_movement():
    state = JobState(status="processing")
    assert state.apply(Event(name="phase", data={}, event_id="9")) is False
    assert state.last_event_id == "9"


def test_completed_records_the_token_and_the_result_path():
    state = JobState()
    state.apply(
        Event(
            name="completed", data={"pages": "2", "token": "tok_1", "result": "/v1/jobs/j/result"}
        )
    )

    assert state.token == "tok_1"
    assert state.result_path == "/v1/jobs/j/result"
    assert state.status == "completed"


def test_an_error_event_records_a_code():
    state = JobState()
    state.apply(Event(name="error", data={"code": "encrypted_pdf"}))

    assert state.status == "failed"
    assert state.error_code == "encrypted_pdf"


def test_an_error_event_without_a_code_falls_back_to_conversion_failed():
    state = JobState()
    state.apply(Event(name="error", data={}))
    assert state.error_code == "conversion_failed"


@pytest.mark.parametrize(
    ("event", "terminal"),
    [
        (Event(name="error", data={"code": "encrypted_pdf"}), True),
        (Event(name="error", data={}), True),
        (Event(name="completed", data={"token": "tok_1"}), True),
        (Event(name="completed", data={"pages": "3"}), False),
        (Event(name="completed", data={"token": ""}), False),
        (Event(name="phase", data={"phase": "processing"}), False),
        (Event(name="page", data={"page": "1"}), False),
    ],
)
def test_is_terminal(event, terminal):
    assert event.is_terminal is terminal
