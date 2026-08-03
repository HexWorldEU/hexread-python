"""Malformed, hostile and merely sloppy event streams.

The job path is the only place this client parses a wire format itself, so it is the place a bad
byte can turn into a crash instead of an error. Every stream here is one a real server, proxy or
dropped connection could produce. None of them may raise anything but a HexReadError, and none may
be mistaken for a completed conversion.

The sync client folds a running generator over the whole stream and the async client buffers one
dispatch block at a time, so the same malformed input is pushed through both.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    JOB_ID,
    KEEPALIVE,
    event_blocks,
    job_response,
    ok_response,
    sse,
    sse_response,
)

from hexread import CapacityError, HexReadError
from hexread._sse import Event, JobState, parse_lines

RESULT = ok_response(pages_remaining=None)


def run_sync(make_api, make_client, blocks, **routes):
    routes.setdefault("result", RESULT)
    api = make_api(convert=job_response(), events=sse_response(*blocks), **routes)
    return api, make_client(api).convert(b"%PDF-1.7")


async def run_async(make_api, make_async_client, blocks, **routes):
    routes.setdefault("result", RESULT)
    api = make_api(convert=job_response(), events=sse_response(*blocks), **routes)
    async with make_async_client(api) as client:
        return api, await client.convert(b"%PDF-1.7")


def completed(**extra) -> str:
    data = {"event": "completed", "pages": "3", "token": "tok_read_once"}
    data.update(extra)
    return sse("completed", data, event_id="9")


# -- noise that must not stop a good conversion -------------------------------------------------

NOISY_STREAMS: dict[str, tuple[str, ...]] = {
    "unknown event names": (
        sse("quantum", {"anything": "at all"}),
        sse("phase", {"phase": "warming"}),
        sse("deprecated", {"event": "deprecated"}),
        completed(),
    ),
    "event with no data": ("event: phase\n\n", completed()),
    "data that is not json": ("event: phase\ndata: definitely not json\n\n", completed()),
    "data that is a json array": ("event: phase\ndata: [1, 2, 3]\n\n", completed()),
    "data that is a json string": ('event: phase\ndata: "warming"\n\n', completed()),
    "data that is json null": ("event: phase\ndata: null\n\n", completed()),
    "interleaved comments": (
        KEEPALIVE,
        ": another comment\n",
        sse("phase", {"phase": "warming"}),
        KEEPALIVE,
        ": still here\n\n",
        completed(),
    ),
    "comment inside an event block": (
        'event: phase\n: mid-block keepalive\ndata: {"phase": "warming"}\n\n',
        completed(),
    ),
    "crlf line endings": (
        'event: phase\r\ndata: {"phase": "warming"}\r\n\r\n',
        "event: completed\r\ndata: "
        + json.dumps({"pages": "3", "token": "tok_read_once"})
        + "\r\n\r\n",
    ),
    "no trailing blank line": (
        sse("phase", {"phase": "warming"}),
        'event: completed\ndata: {"pages": "3", "token": "tok_read_once"}\n',
    ),
    "no space after the colon": (
        'event:phase\ndata:{"phase":"warming"}\n\n',
        'event:completed\ndata:{"pages":"3","token":"tok_read_once"}\n\n',
    ),
    "fields in an unusual order": (
        'data: {"pages": "3", "token": "tok_read_once"}\nid: 7\nevent: completed\n\n',
    ),
    "multi-line data": (
        'event: completed\ndata: {"pages": "3",\ndata:  "token": "tok_read_once"}\n\n',
    ),
    "blank lines everywhere": ("\n", "\n", completed(), "\n", "\n"),
    "extra unknown fields on the event": (
        completed(retry="5000", severity="info", nested='{"a": 1}'),
    ),
    "huge payload": (completed(filler="x" * 1_000_000),),
    "a second completed after the first": (completed(), completed(token="tok_two")),
}


@pytest.mark.parametrize("blocks", NOISY_STREAMS.values(), ids=list(NOISY_STREAMS))
def test_sync_survives_noise_and_still_delivers(make_api, make_client, blocks):
    _, result = run_sync(make_api, make_client, blocks)
    assert result.markdown.startswith("# Quarterly report")
    assert result.job_id == JOB_ID


@pytest.mark.parametrize("blocks", NOISY_STREAMS.values(), ids=list(NOISY_STREAMS))
async def test_async_survives_noise_and_still_delivers(make_api, make_async_client, blocks):
    _, result = await run_async(make_api, make_async_client, blocks)
    assert result.markdown.startswith("# Quarterly report")
    assert result.job_id == JOB_ID


def test_the_result_token_is_taken_from_the_first_completed(make_api, make_client):
    api, _ = run_sync(make_api, make_client, NOISY_STREAMS["a second completed after the first"])
    assert api.only("result").url.params["token"] == "tok_read_once"


def test_a_huge_payload_does_not_end_up_in_the_result(make_api, make_client):
    _, result = run_sync(make_api, make_client, NOISY_STREAMS["huge payload"])
    assert "x" * 1000 not in result.markdown


# -- streams that never deliver a token ---------------------------------------------------------

DEAD_STREAMS: dict[str, tuple[str, ...]] = {
    "nothing at all": (),
    "only comments": (KEEPALIVE, KEEPALIVE),
    "truncated mid event": ('event: completed\ndata: {"pages": "3", "token": "tok',),
    "truncated mid line": ("event: comple",),
    "completed with an empty token": (
        sse("completed", {"event": "completed", "pages": "3", "token": ""}),
    ),
    "completed with no token at all": (sse("completed", {"event": "completed", "pages": "3"}),),
    "data with no event name": ('data: {"pages": "3", "token": "tok_read_once"}\n\n',),
    "progress but never a completion": (
        sse("phase", {"phase": "warming"}),
        sse("page", {"page": "1", "total": "3"}),
    ),
    "a body that is not an event stream at all": ("<html><body>proxy error</body></html>",),
}


@pytest.mark.parametrize("blocks", DEAD_STREAMS.values(), ids=list(DEAD_STREAMS))
def test_sync_gives_up_on_a_stream_that_never_completes(make_api, make_client, blocks, sleeps):
    api = make_api(convert=job_response(), events=sse_response(*blocks))
    with pytest.raises(CapacityError) as caught:
        make_client(api).convert(b"%PDF-1.7")

    assert caught.value.code == "stream_lost"
    assert len(api.sent("events")) <= 8, "reconnects must be bounded"
    assert sum(sleeps) <= 120, f"waited {sum(sleeps)}s between reconnects"


@pytest.mark.parametrize("blocks", DEAD_STREAMS.values(), ids=list(DEAD_STREAMS))
async def test_async_gives_up_on_a_stream_that_never_completes(
    make_api, make_async_client, blocks, async_sleeps
):
    api = make_api(convert=job_response(), events=sse_response(*blocks))
    async with make_async_client(api) as client:
        with pytest.raises(CapacityError) as caught:
            await client.convert(b"%PDF-1.7")

    assert caught.value.code == "stream_lost"
    assert len(api.sent("events")) <= 8
    assert sum(async_sleeps) <= 120


def test_a_data_only_event_never_fetches_a_result(make_api, make_client, sleeps):
    """No event name and no `event` key means the payload is unidentified. Do not act on it."""
    api = make_api(
        convert=job_response(),
        events=sse_response('data: {"pages": "3", "token": "tok_read_once"}\n\n'),
    )
    with pytest.raises(CapacityError):
        make_client(api).convert(b"%PDF-1.7")
    assert api.sent("result") == []


def test_an_error_event_beats_a_reconnect(make_api, make_client, sleeps):
    api = make_api(
        convert=job_response(),
        events=sse_response(sse("error", {"event": "error", "code": "encrypted_pdf"})),
    )
    with pytest.raises(HexReadError) as caught:
        make_client(api).convert(b"%PDF-1.7")

    assert caught.value.code == "encrypted_pdf"
    assert len(api.sent("events")) == 1, "a terminal error must not be retried"


def test_an_error_event_with_no_code_still_raises(make_api, make_client, sleeps):
    api = make_api(convert=job_response(), events=sse_response(sse("error", {"event": "error"})))
    with pytest.raises(HexReadError) as caught:
        make_client(api).convert(b"%PDF-1.7")
    assert caught.value.code == "conversion_failed"


def test_reconnects_resume_from_the_last_event_id(make_api, make_client, sleeps):
    streams = [
        sse_response(sse("page", {"event": "page", "page": "1", "total": "3"}, event_id="17")),
        sse_response(*event_blocks()),
    ]
    api = make_api(convert=job_response(), events=streams, result=RESULT)
    make_client(api).convert(b"%PDF-1.7")

    first, second = api.sent("events")
    assert "last-event-id" not in first.headers
    assert second.headers["last-event-id"] == "17"


# -- the parser on its own ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [""],
        ["", "", ""],
        [":"],
        ["::::"],
        ["event"],
        ["event:"],
        ["data"],
        ["data:"],
        ["id: 1"],
        ["event: x", "data: " + "\ud800"],  # a lone surrogate, if it ever reaches us
        ["\x00\x01\x02"],
        ["event: \x00", "data: {}"],
        ["data: {" * 500],
        ["event: completed"] * 1000,
    ],
    ids=[
        "empty",
        "one blank",
        "all blank",
        "bare colon",
        "only colons",
        "field with no colon",
        "empty event name",
        "data with no colon",
        "empty data",
        "id only",
        "lone surrogate",
        "control bytes",
        "nul in the event name",
        "deeply unbalanced json",
        "a thousand names with no data",
    ],
)
def test_the_parser_never_raises(lines):
    state = JobState()
    for event in parse_lines(lines):
        assert isinstance(event, Event)
        state.apply(event)
    assert isinstance(state.token, str)


def test_an_event_id_is_remembered_even_from_an_event_that_changes_nothing():
    state = JobState()
    list(map(state.apply, parse_lines(["id: 42", "event: quantum", "data: {}", ""])))
    assert state.last_event_id == "42"


def test_page_counts_that_are_not_numbers_are_ignored():
    state = JobState()
    state.pages_done, state.pages_total = 2, 5
    for event in parse_lines(["event: page", 'data: {"page": "many", "total": "3.5"}', ""]):
        state.apply(event)
    assert (state.pages_done, state.pages_total) == (2, 5)


def test_progress_is_monotonic_so_a_replayed_stream_cannot_reset_the_budget():
    """A reconnect that replays `phase` after `page` walks the status backwards.

    Reporting that as movement resets the caller's reconnect budget, and a server that replays its
    buffered prefix on every reconnect would then be reconnected to forever.
    """
    state = JobState()
    moved = []
    for _ in range(3):
        for event in parse_lines(
            [
                "event: phase",
                'data: {"phase": "warming"}',
                "",
                "event: page",
                'data: {"page": "1", "total": "3"}',
                "",
            ]
        ):
            moved.append(state.apply(event))

    assert moved[:2] == [True, True], "the first pass is real progress"
    assert not any(moved[2:]), "replaying the same events is not progress"


def test_a_stream_that_replays_itself_is_abandoned(make_api, make_client, sleeps):
    api = make_api(
        convert=job_response(),
        events=sse_response(
            sse("phase", {"event": "phase", "phase": "warming"}),
            sse("page", {"event": "page", "page": "1", "total": "3"}),
        ),
    )
    with pytest.raises(CapacityError) as caught:
        make_client(api).convert(b"%PDF-1.7")

    assert caught.value.code == "stream_lost"
    assert len(api.sent("events")) <= 8


def test_a_completed_without_a_token_is_not_terminal():
    (event,) = parse_lines(["event: completed", 'data: {"pages": "3"}', ""])
    assert not event.is_terminal


def test_an_error_is_terminal_even_with_no_data():
    (event,) = parse_lines(["event: error", ""])
    assert event.is_terminal
