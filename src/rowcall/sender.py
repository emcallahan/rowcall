"""Send one rendered request and describe what happened."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from . import __version__
from .template import RenderedRequest

USER_AGENT = f"rowcall/{__version__}"


@dataclass
class SendResult:
    status: int | None  # HTTP status code, or None if no response came back
    ok: bool
    response_text: str
    error: str
    elapsed_ms: int


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
    try:
        response = session.request(request.method, request.url, **kwargs)
    except requests.ConnectTimeout:
        # Could not even reach the server (Windows reports a closed port this way).
        return SendResult(None, False, "", f"could not connect within {timeout:g} seconds", _ms_since(started))
    except requests.Timeout:
        return SendResult(None, False, "", f"no response after {timeout:g} seconds", _ms_since(started))
    except requests.ConnectionError as exc:
        return SendResult(None, False, "", f"could not connect: {_short(exc)}", _ms_since(started))
    except requests.RequestException as exc:
        return SendResult(None, False, "", f"request failed: {_short(exc)}", _ms_since(started))

    ok = 200 <= response.status_code < 300
    error = "" if ok else f"HTTP {response.status_code} {response.reason or ''}".strip()
    return SendResult(response.status_code, ok, response.text, error, _ms_since(started))


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _short(exc: Exception) -> str:
    # requests wraps errors several layers deep; the last part is usually the useful one.
    text = str(exc)
    return text if len(text) <= 300 else text[:300] + "..."
