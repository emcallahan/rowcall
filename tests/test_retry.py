from datetime import datetime, timezone

import pytest

from rowcall.retry import RateLimiter, RetryingSender, RetryPolicy, is_retryable, retry_delay
from rowcall.sender import (
    FAIL_CONNECT,
    FAIL_DROPPED,
    FAIL_HTTP,
    FAIL_TIMEOUT,
    SendResult,
    parse_retry_after,
)
from rowcall.template import RenderedRequest


def result(status=None, failure=FAIL_HTTP, retry_after=None, ok=False):
    return SendResult(status, ok, "", "" if ok else f"HTTP {status}", 5, failure, retry_after)


OK = SendResult(200, True, "", "", 5)


@pytest.mark.parametrize(
    "method, res, expected",
    [
        ("POST", OK, False),
        ("POST", result(failure=FAIL_CONNECT), True),  # never reached the server
        ("POST", result(429), True),  # server says it did nothing
        ("POST", result(503), True),
        ("POST", result(500), False),  # a real error: retrying will not help
        ("POST", result(400), False),
        # The server may already have acted on these, so only repeatable methods retry.
        ("POST", result(502), False),
        ("POST", result(failure=FAIL_TIMEOUT), False),
        ("PATCH", result(failure=FAIL_DROPPED), False),
        ("PUT", result(502), True),
        ("DELETE", result(504), True),
        ("GET", result(failure=FAIL_TIMEOUT), True),
        ("DELETE", result(failure=FAIL_DROPPED), True),
    ],
)
def test_is_retryable(method, res, expected):
    assert is_retryable(method, res) is expected


def test_retry_delay_doubles_and_caps():
    policy = RetryPolicy(base_delay=1, max_wait=5)
    assert [retry_delay(policy, n, None) for n in (1, 2, 3, 4)] == [1, 2, 4, 5]


def test_retry_delay_uses_retry_after():
    assert retry_delay(RetryPolicy(), 1, 7.0) == 7.0


def test_parse_retry_after():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:10 GMT", now=now) == 10.0
    assert parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=now) == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def test_rate_limiter_spaces_requests():
    clock = FakeClock()
    limiter = RateLimiter(4, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.wait()
    assert clock.sleeps == [0.25, 0.25]


def test_rate_limiter_does_not_wait_when_requests_are_slow():
    clock = FakeClock()
    limiter = RateLimiter(4, clock=clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 1  # the request itself took a second
    limiter.wait()
    assert clock.sleeps == []


def test_rate_zero_means_no_limit():
    clock = FakeClock()
    limiter = RateLimiter(0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.wait()
    assert clock.sleeps == []


def make_sender(responses, max_retries=3, max_wait=60):
    calls = []
    sleeps = []

    def send_fn(request):
        calls.append(request)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    sender = RetryingSender(
        send_fn,
        RetryPolicy(max_retries=max_retries, base_delay=1, max_wait=max_wait),
        RateLimiter(0),
        sleep=sleeps.append,
    )
    return sender, calls, sleeps


REQ = RenderedRequest("POST", "https://x", {}, None)


def test_retries_until_success():
    sender, calls, sleeps = make_sender([result(429), result(503), OK])
    res, attempts = sender.send(REQ)
    assert res.ok and attempts == 3 and len(calls) == 3
    assert sleeps == [1, 2]


def test_gives_up_after_max_retries():
    sender, calls, sleeps = make_sender([result(429)], max_retries=2)
    res, attempts = sender.send(REQ)
    assert not res.ok and attempts == 3
    assert sleeps == [1, 2]


def test_does_not_retry_unsafe_failure():
    sender, calls, sleeps = make_sender([result(failure=FAIL_TIMEOUT)])
    res, attempts = sender.send(REQ)
    assert attempts == 1 and sleeps == []


def test_gives_up_when_server_asks_to_wait_too_long():
    sender, calls, sleeps = make_sender([result(503, retry_after=600)], max_wait=60)
    res, attempts = sender.send(REQ)
    assert attempts == 1 and sleeps == []
    assert "longer than --max-wait" in res.error
