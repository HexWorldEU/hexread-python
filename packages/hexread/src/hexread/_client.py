"""The synchronous client.

Handles the inline-or-job fork, the event stream and the one-time result token, so a caller
never writes a polling loop.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
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


class HexRead:
    """A HexRead API client.

    The key is resolved once, at construction: `api_key`, then HEXREAD_API_KEY, then the
    credential the HexRead CLI wrote at login.
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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = resolve_api_key(api_key)
        self._base_url = resolve_base_url(base_url)
        self._origin = origin_of(self._base_url)
        self._model = model
        self._lang = lang
        self._max_retries = max_retries
        self._timeout = httpx.Timeout(timeout) if timeout is not None else t.CONVERT_TIMEOUT
        self._http = httpx.Client(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": t.USER_AGENT},
        )
        self._owns_http = True

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> HexRead:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def convert(
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
        """Convert one document and return the result.

        `source` is a path, raw bytes, or an open binary file. Blocks until the markdown exists,
        whether the server answered inline or handed back a job.
        """
        # Open the file inside the try, so the handle closes on every exit path.
        callback, printer = resolve_progress(progress, on_progress)
        upload: t.Upload | None = None
        key = idempotency_key or t.new_idempotency_key()

        try:
            upload = t.prepare_upload(source, name=name)
            label = upload.name
            if callback:
                callback(Progress(phase="uploading", source=label))
            response = self._post_convert(upload, model, lang, prefer, key)

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
                            phase="completed",
                            pages_done=total,
                            pages_total=total,
                            source=label,
                        )
                    )
                return result

            return self._await_job(t.decode_payload(response), label=label, callback=callback)
        finally:
            if upload is not None:
                upload.release()
            if printer:
                printer.close()

    def convert_many(
        self,
        sources: Iterable[Any],
        *,
        max_workers: int = 2,
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> list[ConvertResult | HexReadError]:
        """Convert several documents, a few at a time, returning results in input order.

        A failure is returned in place rather than losing the batch, unless `raise_on_error=True`.
        Keep `max_workers` at or below `usage().concurrency_limit`; above it the server answers 429.
        """
        items: Sequence[Any] = list(sources)
        if not items:
            return []
        workers = max(1, min(int(max_workers), len(items)))

        def one(item: Any) -> ConvertResult | HexReadError:
            try:
                return self.convert(item, **kwargs)
            except HexReadError as exc:
                if raise_on_error:
                    raise
                return exc

        if workers == 1:
            return [one(item) for item in items]
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            return list(pool.map(one, items))
        finally:
            # map() queues everything up front; drop what has not started, since each one
            # would be a billed conversion the caller no longer wants.
            pool.shutdown(wait=True, cancel_futures=True)

    def usage(self) -> Usage:
        """Current page allowance, concurrency, and which models this plan may request."""
        response = self._http.get(
            f"{self._base_url}/usage",
            headers=t.auth_headers(self._api_key),
            timeout=t.CONTROL_TIMEOUT,
        )
        t.raise_for_response(response)
        return Usage.from_payload(t.decode_payload(response))

    def cancel(self, job_id: str) -> None:
        """Cancel a queued or running job. Releases its reservation without a charge."""
        response = self._http.delete(
            f"{self._base_url}/jobs/{job_id}",
            headers=t.auth_headers(self._api_key),
            timeout=t.CONTROL_TIMEOUT,
        )
        t.raise_for_response(response)

    def _post_convert(
        self,
        upload: t.Upload,
        model: str | None,
        lang: str | None,
        prefer: str | None,
        idempotency_key: str,
    ) -> httpx.Response:
        fields = t.convert_fields(
            model=model or self._model,
            lang=lang or self._lang,
            prefer=prefer,
        )
        headers = t.auth_headers(self._api_key, {"Idempotency-Key": idempotency_key})
        attempt = 0
        while True:
            response = self._http.post(
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
            # The body is spent; a source that cannot rewind would retry as an empty upload.
            if not upload.rewind():
                raise exc
            time.sleep(t.retry_delay(attempt, exc))
            attempt += 1

    def _await_job(
        self,
        job: dict[str, Any],
        *,
        label: str,
        callback: ProgressCallback | None,
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

        deadline = time.monotonic() + t.SSE_MAX_TOTAL_SECONDS
        stalled = 0
        while True:
            progressed = self._stream_events(events_url, state, label, callback)
            if state.token:
                break
            if state.error_code:
                raise error_from_job_code(state.error_code, job_id)
            if time.monotonic() > deadline:
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
            time.sleep(min(t.MAX_BACKOFF, max(1, stalled) * t.SSE_RETRY_BASE))

        result_url = t.resolve_job_url(
            self._origin, self._base_url, job_id, state.result_path, "/result"
        )
        return self._fetch_result(result_url, state.token, job_id=job_id, label=label)

    def _stream_events(
        self,
        url: str,
        state: JobState,
        label: str,
        callback: ProgressCallback | None,
    ) -> bool:
        """Consume events until the stream ends. Returns whether anything was learned."""
        headers = t.auth_headers(self._api_key)
        if state.last_event_id:
            headers["Last-Event-ID"] = state.last_event_id
        progressed = False
        try:
            with self._http.stream(
                "GET", url, headers=headers, timeout=t.STREAM_TIMEOUT
            ) as response:
                if not response.is_success:
                    response.read()
                    raise error_from_response(
                        response.status_code, response.content, response.headers
                    )
                for event in parse_lines(response.iter_lines()):
                    if state.apply(event):
                        progressed = True
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
                        return True
        except HexReadError:
            raise
        except httpx.HTTPError:
            # A dropped stream is normal on a long conversion; the caller decides on a reconnect.
            return progressed
        return progressed

    def _fetch_result(self, url: str, token: str, *, job_id: str, label: str) -> ConvertResult:
        response = self._http.get(
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
