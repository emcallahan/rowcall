"""Pace requests and retry the ones that are safe to retry.

The key rule: never retry in a way that could do something twice.

- If the request never reached the server, retrying is always safe.
- If the server answered 429 (too many requests) or 503 (unavailable), it is
  telling us it did not do the work, so retrying is safe.
- If the request timed out, the connection dropped, or a gateway answered 502
  or 504, the server may already have done the work. Retrying is only safe for
  methods that give the same result when repeated (GET, PUT, DELETE, ...), not
  for POST or PATCH, which could create duplicates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .sender import FAIL_CONNECT, FAIL_DROPPED, FAIL_TIMEOUT, SendResult
from .template import RenderedRequest

RETRY_ANY_METHOD = {429, 503}
RETRY_REPEATABLE_ONLY = {502, 504}
REPEATABLE_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0  # first wait when the server gives no Retry-After; doubles each time
    max_wait: float = 60.0  # never wait longer than this for one retry


def is_retryable(method: str, result: SendResult) -> bool:
    if result.ok:
        return False
    if result.failure == FAIL_CONNECT:
        return True
    if result.status in RETRY_ANY_METHOD:
        return True
    if method.upper() in REPEATABLE_METHODS:
        return result.status in RETRY_REPEATABLE_ONLY or result.failure in (FAIL_TIMEOUT, FAIL_DROPPED)
    return False


def retry_delay(policy: RetryPolicy, retry_number: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return min(policy.base_delay * (2 ** (retry_number - 1)), policy.max_wait)


class RateLimiter:
    """Spaces out request starts so there are at most `per_second` per second."""

    def __init__(
        self,
        per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = None

    def wait(self) -> None:
        if self.interval == 0:
            return
        now = self.clock()
        if self._next is not None and self._next > now:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


SendFn = Callable[..., SendResult]
OnRetry = Callable[[int, SendResult, float], None]


class RetryingSender:
    def __init__(
        self,
        send_fn: SendFn,
        policy: RetryPolicy,
        limiter: RateLimiter,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: OnRetry | None = None,
    ):
        self.send_fn = send_fn
        self.policy = policy
        self.limiter = limiter
        self.sleep = sleep
        self.on_retry = on_retry
        # True once a request for the current row has gone out. Used after
        # Ctrl+C to tell "not sent" apart from "may have been sent".
        self.attempted = False

    def send(self, request: RenderedRequest) -> tuple[SendResult, int]:
        self.attempted = False
        attempts = 0
        while True:
            self.limiter.wait()
            self.attempted = True
            attempts += 1
            result = self.send_fn(request)

            if attempts > self.policy.max_retries or not is_retryable(request.method, result):
                return result, attempts

            delay = retry_delay(self.policy, attempts, result.retry_after)
            if delay > self.policy.max_wait:
                result.error += f" (server asked to wait {delay:g}s, longer than --max-wait)"
                return result, attempts

            if self.on_retry:
                self.on_retry(attempts, result, delay)
            self.sleep(delay)
