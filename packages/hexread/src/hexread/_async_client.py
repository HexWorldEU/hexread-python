"""The asynchronous client.

Behaviour matches `HexRead` exactly; only the I/O is awaited.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from . import _transport as t
from ._auth import origin_of, resolve_api_key, resolve_base_url
from ._errors import (
    CapacityError,
    HexReadError,
    error_from_job_code,
    error_from_response,
)
from ._models import ConvertResult, Progress, Usage
from ._progress import ProgressCallback, resolve_progress
from ._sse import JobState, parse_lines


class AsyncHexRead:
    """The async counterpart of `HexRead`.

    async with AsyncHexRead() as client:
        result = await client.convert("report.pdf")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        lang: str | None = None,
        timeout: float | None = None,
        max_retries: int = t.MAX_RETRIES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = resolve_api_key(api_key)
        self._base_url = resolve_base_url(base_url)
        self._origin = origin_of(self._base_url)
        self._model = model
        self._lang = lang
        self._max_retries = max_retries
        self._timeout = httpx.Timeout(timeout) if timeout is not None else t.CONVERT_TIMEOUT
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": t.USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncHexRead:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def convert(
        self,
        source: Any,
        *,
        model: str | None = None,
        lang: str | None = None,
        prefer: str | None = None,
        name: str | None = None,
        progress: bool | ProgressCallback | None = None,
        on_progress: ProgressCallback | None = None,
        idempotency_key: str | None = None,
    ) -> ConvertResult:
        """Convert one document. See `HexRead.convert` for the semantics."""
        callback, printer = resolve_progress(progress, on_progress)
        upload: t.Upload | None = None
        key = idempotency_key or t.new_idempotency_key()

        try:
            upload = t.prepare_upload(source, name=name)
            label = upload.name
            if callback:
                callback(Progress(phase="uploading", source=label))
            response = await self._post_convert(upload, model, lang, prefer, key)

            if response.status_code == httpx.codes.OK:
                result = t.build_result(
                    t.decode_payload(response),
                    response.text,
                    source=label,
                    pages_remaining=t.quota_remaining(response.headers),
                )
                if callback:
                    total = result.page_count
                    callback(
                        Progress(
                            phase="completed", pages_done=total, pages_total=total, source=label
                        )
                    )
                return result

            return await self._await_job(t.decode_payload(response), label=label, callback=callback)
        finally:
            if upload is not None:
                upload.release()
            if printer:
                printer.close()

    async def convert_many(
        self,
        sources: Iterable[Any],
        *,
        max_workers: int = 2,
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> list[ConvertResult | HexReadError]:
        """Convert several documents with bounded concurrency, preserving input order."""
        items: Sequence[Any] = list(sources)
        if not items:
            return []
        limit = asyncio.Semaphore(max(1, int(max_workers)))

        async def one(item: Any) -> ConvertResult | HexReadError:
            async with limit:
                try:
                    return await self.convert(item, **kwargs)
                except HexReadError as exc:
                    if raise_on_error:
                        raise
                    return exc

        tasks = [asyncio.ensure_future(one(item)) for item in items]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # gather() re-raises but leaves the siblings running. They would outlive aclose()
            # and bill for work nobody is waiting on.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def usage(self) -> Usage:
        """Current page allowance, concurrency, and which models this plan may request."""
        response = await self._http.get(
            f"{self._base_url}/usage",
            headers=t.auth_headers(self._api_key),
            timeout=t.CONTROL_TIMEOUT,
        )
        t.raise_for_response(response)
        return Usage.from_payload(t.decode_payload(response))

    async def cancel(self, job_id: str) -> None:
        """Cancel a queued or running job. Releases its reservation without a charge."""
        response = await self._http.delete(
            f"{self._base_url}/jobs/{job_id}",
            headers=t.auth_headers(self._api_key),
            timeout=t.CONTROL_TIMEOUT,
        )
        t.raise_for_response(response)

    async def _post_convert(
        self,
        upload: t.Upload,
        model: str | None,
        lang: str | None,
        prefer: str | None,
        idempotency_key: str,
    ) -> httpx.Response:
        fields = t.convert_fields(
            model=model or self._model, lang=lang or self._lang, prefer=prefer
        )
        headers = t.auth_headers(self._api_key, {"Idempotency-Key": idempotency_key})
        attempt = 0
        while True:
            response = await self._http.post(
                f"{self._base_url}/convert",
                headers=headers,
                files=upload.as_files(),
                data=fields,
                timeout=self._timeout,
            )
            if response.is_success:
                return response
            exc = error_from_response(response.status_code, response.content, response.headers)
            if not t.should_retry(exc, attempt, self._max_retries):
                raise exc
            # A body that cannot rewind would re-upload empty. See HexRead._post_convert.
            if not upload.rewind():
                raise exc
            await asyncio.sleep(t.retry_delay(attempt, exc))
            attempt += 1

    async def _await_job(
        self, job: dict[str, Any], *, label: str, callback: ProgressCallback | None
    ) -> ConvertResult:
        job_id = str(job.get("job_id", ""))
        raw_links = job.get("links")
        links = raw_links if isinstance(raw_links, dict) else {}
        events_url = t.resolve_job_url(
            self._origin, self._base_url, job_id, str(links.get("events", "")), "/events"
        )
        state = JobState(status=str(job.get("status", "queued")))
        if callback:
            callback(Progress(phase=state.status, source=label))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + t.SSE_MAX_TOTAL_SECONDS
        stalled = 0
        while True:
            progressed = await self._stream_events(events_url, state, label, callback)
            if state.token:
                break
            if state.error_code:
                raise error_from_job_code(state.error_code, job_id)
            if loop.time() > deadline:
                raise CapacityError(
                    f"Gave up waiting for job {job_id} after two hours.",
                    code="conversion_timeout",
                )
            stalled = 0 if progressed else stalled + 1
            if stalled > t.SSE_MAX_STALLED_RETRIES:
                raise CapacityError(
                    f"Lost the connection to job {job_id} and it made no further progress. "
                    "The job may still be running, but a finished result is only held briefly.",
                    code="stream_lost",
                )
            await asyncio.sleep(min(t.MAX_BACKOFF, max(1, stalled) * t.SSE_RETRY_BASE))

        result_url = t.resolve_job_url(
            self._origin, self._base_url, job_id, state.result_path, "/result"
        )
        return await self._fetch_result(result_url, state.token, job_id=job_id, label=label)

    async def _stream_events(
        self, url: str, state: JobState, label: str, callback: ProgressCallback | None
    ) -> bool:
        headers = t.auth_headers(self._api_key)
        if state.last_event_id:
            headers["Last-Event-ID"] = state.last_event_id
        progressed = False
        try:
            async with self._http.stream(
                "GET", url, headers=headers, timeout=t.STREAM_TIMEOUT
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise error_from_response(
                        response.status_code, response.content, response.headers
                    )
                # Decode a block at a time; re-parsing the buffer would replay earlier events.
                block: list[str] = []
                async for line in response.aiter_lines():
                    block.append(line)
                    if line != "":
                        continue
                    terminal, moved = self._drain(block, state, label, callback)
                    progressed = progressed or moved
                    block.clear()
                    if terminal:
                        return True
                if block:
                    terminal, moved = self._drain(block, state, label, callback)
                    progressed = progressed or moved
                    if terminal:
                        return True
        except HexReadError:
            raise
        except httpx.HTTPError:
            return progressed
        return progressed

    def _drain(
        self,
        block: list[str],
        state: JobState,
        label: str,
        callback: ProgressCallback | None,
    ) -> tuple[bool, bool]:
        """Decode one dispatched SSE block. Returns (stream is finished, state moved)."""
        moved = False
        for event in parse_lines(block):
            if state.apply(event):
                moved = True
                if callback:
                    callback(
                        Progress(
                            phase=state.status,
                            pages_done=state.pages_done,
                            pages_total=state.pages_total,
                            source=label,
                        )
                    )
            if event.is_terminal:
                return True, moved
        return False, moved

    async def _fetch_result(
        self, url: str, token: str, *, job_id: str, label: str
    ) -> ConvertResult:
        response = await self._http.get(
            url,
            params={"token": token},
            headers={"Accept": "application/json", "User-Agent": t.USER_AGENT},
            timeout=t.CONTROL_TIMEOUT,
        )
        if not response.is_success:
            raise t.unwrap_result_error(
                error_from_response(response.status_code, response.content, response.headers),
                job_id,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return t.build_result(
            payload,
            response.text,
            source=label,
            pages_remaining=t.quota_remaining(response.headers),
            job_id=job_id,
        )
