"""Send one rendered request and describe what happened."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from . import __version__
from .template import RenderedRequest

USER_AGENT = f"rowcall/{__version__}"

# How a request failed, which decides whether it is safe to retry.
FAIL_NONE = ""
FAIL_CONNECT = "connect"  # never reached the server: always safe to retry
FAIL_TIMEOUT = "timeout"  # sent, but no answer: the server may have acted on it
FAIL_DROPPED = "dropped"  # connection broke mid-request: the server may have acted on it
FAIL_HTTP = "http"  # the server answered with an error status
FAIL_OTHER = "other"


@dataclass
class SendResult:
    status: int | None  # HTTP status code, or None if no response came back
    ok: bool
    response_text: str
    error: str
    elapsed_ms: int
    failure: str = FAIL_NONE
    retry_after: float | None = None  # seconds the server asked us to wait, if it said


def send(request: RenderedRequest, session: requests.Session, timeout: float) -> SendResult:
    headers = dict(request.headers)
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = USER_AGENT

    kwargs = {"headers": headers, "timeout": timeout}
    if isinstance(request.body, (dict, list)):
        kwargs["json"] = request.body
    elif request.body is not None:
        kwargs["data"] = request.body.encode("utf-8")

    started = time.monotonic()

    def failed(message: str, failure: str) -> SendResult:
        return SendResult(None, False, "", message, _ms_since(started), failure)

    try:
        response = session.request(request.method, request.url, **kwargs)
    except requests.ConnectTimeout:
        # Could not even reach the server (Windows reports a closed port this way).
        return failed(f"could not connect within {timeout:g} seconds", FAIL_CONNECT)
    except requests.Timeout:
        return failed(f"no response after {timeout:g} seconds", FAIL_TIMEOUT)
    except requests.ConnectionError as exc:
        if _never_reached(exc):
            return failed(f"could not connect: {_short(exc)}", FAIL_CONNECT)
        return failed(f"connection dropped: {_short(exc)}", FAIL_DROPPED)
    except requests.RequestException as exc:
        return failed(f"request failed: {_short(exc)}", FAIL_OTHER)

    ok = 200 <= response.status_code < 300
    error = "" if ok else f"HTTP {response.status_code} {response.reason or ''}".strip()
    return SendResult(
        response.status_code,
        ok,
        response.text,
        error,
        _ms_since(started),
        FAIL_NONE if ok else FAIL_HTTP,
        parse_retry_after(response.headers.get("Retry-After")),
    )


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max((when - now).total_seconds(), 0.0)


def _never_reached(exc: requests.ConnectionError) -> bool:
    reason = exc.args[0] if exc.args else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return isinstance(reason, NewConnectionError)


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _short(exc: Exception) -> str:
    # requests wraps errors several layers deep; the last part is usually the useful one.
    text = str(exc)
    return text if len(text) <= 300 else text[:300] + "..."
