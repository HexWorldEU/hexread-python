"""Batches and shared clients under contention, sync and async.

Two guarantees a caller relies on and cannot check for themselves: results come back in input
order however the work interleaves, and one client driven from several threads or tasks never
mixes one conversion's state into another's. The failure modes here are silent, so the assertions
are on identity - which document produced which markdown - rather than on counts alone.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from conftest import convert_payload, error_response, ok_response

from hexread import HexReadError, PermissionError, ValidationError

DOCS = [f"doc-{i:03d}".encode() for i in range(24)]


def echo(request: httpx.Request) -> httpx.Response:
    """Answer with the name of the document that was uploaded, so results can be traced back."""
    from conftest import file_part

    name = file_part(request).value.decode("utf-8", "replace")
    return httpx.Response(
        200,
        json=convert_payload(
            f"# {name}", pages=[{"index": 0, "markdown": name}], meta={"pages": 1}
        ),
    )


def failing_echo(fail_on: set[int]):
    """Like `echo`, but refuses the documents whose index is in `fail_on`."""

    def handler(request: httpx.Request) -> httpx.Response:
        from conftest import file_part

        name = file_part(request).value.decode()
        if int(name.rsplit("-", 1)[1]) in fail_on:
            return error_response(403, type="permission_error", code="forbidden", message=name)
        return echo(request)

    return handler


# -- convert_many, sync ---------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 2, 3, 8, 64])
def test_results_stay_in_input_order_whatever_the_worker_count(make_api, make_client, workers):
    api = make_api(convert=echo)
    results = make_client(api).convert_many(DOCS, max_workers=workers)

    assert [r.markdown for r in results] == [f"# {d.decode()}" for d in DOCS]
    assert len(api.sent("convert")) == len(DOCS)


@pytest.mark.parametrize("workers", [0, -1, 1])
def test_a_useless_worker_count_still_converts(make_api, make_client, workers):
    results = make_client(make_api(convert=echo)).convert_many(DOCS[:3], max_workers=workers)
    assert [r.markdown for r in results] == ["# doc-000", "# doc-001", "# doc-002"]


def test_an_empty_batch_sends_nothing(make_api, make_client):
    api = make_api(convert=echo)
    assert make_client(api).convert_many([], max_workers=4) == []
    assert api.requests == []


def test_a_batch_of_one_is_still_a_list(make_api, make_client):
    results = make_client(make_api(convert=echo)).convert_many([DOCS[0]], max_workers=8)
    assert len(results) == 1
    assert results[0].markdown == "# doc-000"


def test_a_generator_of_sources_is_accepted(make_api, make_client):
    results = make_client(make_api(convert=echo)).convert_many(
        (doc for doc in DOCS[:4]), max_workers=2
    )
    assert [r.markdown for r in results] == [f"# doc-{i:03d}" for i in range(4)]


@pytest.mark.parametrize("workers", [1, 4])
def test_failures_are_returned_in_place_and_in_order(make_api, make_client, workers):
    api = make_api(convert=failing_echo({1, 4, 5, 23}))
    results = make_client(api).convert_many(DOCS, max_workers=workers)

    assert len(results) == len(DOCS)
    for i, item in enumerate(results):
        if i in {1, 4, 5, 23}:
            assert isinstance(item, PermissionError)
        else:
            assert item.markdown == f"# doc-{i:03d}"


@pytest.mark.parametrize("workers", [1, 4])
def test_raise_on_error_raises_the_first_failure(make_api, make_client, workers):
    api = make_api(convert=failing_echo({2}))
    with pytest.raises(PermissionError):
        make_client(api).convert_many(DOCS, max_workers=workers, raise_on_error=True)


def test_raise_on_error_stops_converting_the_rest(make_api, make_client):
    """Every queued item is a billed conversion. Abandoning the batch has to abandon them too."""
    served = []
    lock = threading.Lock()

    def slow_echo(request: httpx.Request) -> httpx.Response:
        from conftest import file_part

        name = file_part(request).value.decode()
        with lock:
            served.append(name)
        if name == "doc-000":
            return error_response(403, type="permission_error", code="forbidden")
        time.sleep(0.05)
        return echo(request)

    api = make_api(convert=slow_echo)
    with pytest.raises(PermissionError):
        make_client(api).convert_many(DOCS, max_workers=2, raise_on_error=True)

    assert len(served) < len(DOCS), f"the whole batch ran anyway: {served}"


def test_a_non_hexread_failure_is_not_swallowed_by_the_batch(make_api, make_client, tmp_path):
    """A missing file is the caller's bug, not the API's. It must not look like a conversion."""
    with pytest.raises(FileNotFoundError):
        make_client(make_api(convert=echo)).convert_many(
            [DOCS[0], tmp_path / "nope.pdf"], max_workers=1
        )


# -- one client, many threads ----------------------------------------------------------------------


def test_a_shared_client_keeps_each_conversion_separate(make_api, make_client):
    api = make_api(convert=echo)
    client = make_client(api)
    results: dict[int, str] = {}
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(index: int) -> None:
        try:
            start.wait(timeout=5)
            for round_number in range(6):
                name = f"t{index}-r{round_number}".encode()
                result = client.convert(name, name=f"t{index}.pdf")
                assert result.markdown == f"# {name.decode()}"
                assert result.source == f"t{index}.pdf"
                results[index * 100 + round_number] = result.markdown
        except BaseException as exc:  # noqa: BLE001 - the test reports it, the thread cannot
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 48
    keys = [r.headers["idempotency-key"] for r in api.sent("convert")]
    assert len(set(keys)) == 48, "two conversions shared an idempotency key"


def test_a_shared_client_survives_a_storm_of_failures(make_api, make_client):
    api = make_api(convert=failing_echo(set(range(0, 24, 2))))
    client = make_client(api)
    seen: list[object] = []

    def worker(index: int) -> None:
        try:
            seen.append(client.convert(f"doc-{index:03d}".encode()))
        except HexReadError as exc:
            seen.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(seen) == 24
    assert sum(isinstance(item, HexReadError) for item in seen) == 12


def test_closing_a_client_twice_is_harmless(make_api, make_client):
    client = make_client(make_api(convert=echo))
    client.close()
    client.close()
    with pytest.raises(RuntimeError):
        client.convert(DOCS[0])


# -- the async client ------------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 2, 8, 64])
async def test_async_results_stay_in_input_order(make_api, make_async_client, workers):
    api = make_api(convert=echo)
    async with make_async_client(api) as client:
        results = await client.convert_many(DOCS, max_workers=workers)

    assert [r.markdown for r in results] == [f"# {d.decode()}" for d in DOCS]


async def test_async_empty_and_single_batches(make_api, make_async_client):
    async with make_async_client(make_api(convert=echo)) as client:
        assert await client.convert_many([]) == []
        assert len(await client.convert_many([DOCS[0]], max_workers=9)) == 1


async def test_async_failures_are_returned_in_place(make_api, make_async_client):
    api = make_api(convert=failing_echo({0, 3}))
    async with make_async_client(api) as client:
        results = await client.convert_many(DOCS[:5], max_workers=3)

    assert isinstance(results[0], PermissionError)
    assert isinstance(results[3], PermissionError)
    assert results[1].markdown == "# doc-001"


async def test_async_raise_on_error_leaves_no_task_behind(make_api, make_async_client):
    """gather() re-raises the first failure. Its siblings are billed work: cancel them."""
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        from conftest import file_part

        name = file_part(request).value.decode()
        if name == "doc-000":
            await started.wait()
            return error_response(400, type="validation_error", code="invalid_request")
        await started.wait()
        await asyncio.sleep(5)
        raise AssertionError("a sibling was allowed to finish")

    api = make_api(convert=handler)
    async with make_async_client(api) as client:
        task = asyncio.ensure_future(
            client.convert_many(DOCS[:6], max_workers=6, raise_on_error=True)
        )
        await asyncio.sleep(0)
        started.set()
        with pytest.raises(ValidationError):
            await task

        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftovers == [], f"tasks outlived the batch: {leftovers}"


async def test_cancelling_a_batch_cancels_its_conversions(make_api, make_async_client):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("the batch should have been cancelled")

    api = make_api(convert=handler)
    async with make_async_client(api) as client:
        task = asyncio.ensure_future(client.convert_many(DOCS[:5], max_workers=5))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftovers == []


async def test_gather_over_many_separate_conversions(make_api, make_async_client):
    api = make_api(convert=echo)
    async with make_async_client(api) as client:
        results = await asyncio.gather(*(client.convert(doc) for doc in DOCS))

    assert [r.markdown for r in results] == [f"# {d.decode()}" for d in DOCS]
    keys = {r.headers["idempotency-key"] for r in api.sent("convert")}
    assert len(keys) == len(DOCS)


async def test_a_cancelled_conversion_leaves_the_client_usable(make_api, make_async_client):
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if not gate.is_set():
            await asyncio.sleep(30)
        return echo(request)

    async with make_async_client(make_api(convert=handler)) as client:
        task = asyncio.ensure_future(client.convert(DOCS[0]))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        gate.set()
        assert (await client.convert(DOCS[1])).markdown == "# doc-001"


async def test_the_async_client_refuses_work_after_aclose(make_api, make_async_client):
    client = make_async_client(make_api(convert=echo))
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client.convert(DOCS[0])


async def test_a_batch_bigger_than_the_worker_limit_is_paced(make_api, make_async_client):
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.005)
            return ok_response()
        finally:
            in_flight -= 1

    async with make_async_client(make_api(convert=handler)) as client:
        await client.convert_many(DOCS, max_workers=3)

    assert peak <= 3, f"{peak} conversions were in flight at once"
