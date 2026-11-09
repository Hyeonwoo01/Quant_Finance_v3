"""수집 클라이언트 공통 기능 (가이드 6-3). 네트워크 없이 가짜 transport로 확인한다."""
import logging
import random
from datetime import datetime, timedelta, timezone

import pytest

from collectors.base_client import (BaseClient, BudgetExhausted, CallBudget, IncompletePagination, RequestFailed,
                                    SchemaChanged, TokenBucket, check_pagination, check_schema, parse_retry_after,
                                    redact)

SECRET = "SECRET-KEY-1234567890"
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


class FakeTransport:
    """미리 정한 응답(또는 예외)을 차례로 돌려주고, 받은 요청을 기록한다."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(transport, **kw):
    sleeps = []
    c = BaseClient("https://api.example.invalid/api/", transport=transport, sleep=sleeps.append,
                   rng=random.Random(0), now=lambda: NOW, **kw)
    return c, sleeps


def test_success_sends_secret_but_never_exposes_it():
    t = FakeTransport((200, {}, b'{"status": "000"}'))
    c, sleeps = client(t)
    r = c.get("list.json", {"crtfc_key": SECRET, "bgn_de": "20261106", "page_no": 1, "unused": None})
    assert t.calls == [("https://api.example.invalid/api/list.json", {"crtfc_key": SECRET, "bgn_de": "20261106", "page_no": "1"})]
    assert r.status == 200 and r.json() == {"status": "000"} and r.attempts == 1 and sleeps == []
    assert r.params["crtfc_key"] == "***" and SECRET not in repr(r) and "?" not in r.url


def test_429_follows_retry_after_and_retry_counts_against_budget():
    t = FakeTransport((429, {"Retry-After": "2"}, b""), (200, {}, b"ok"))
    budget = CallBudget(10)
    c, sleeps = client(t, budget=budget)
    r = c.get("x", {})
    assert r.body == b"ok" and r.attempts == 2 and sleeps == [2.0] and budget.used == 2


def test_5xx_and_network_errors_back_off_then_fail(caplog):
    t = FakeTransport((503, {}, b""), TimeoutError("timed out"), (500, {}, b""))
    c, sleeps = client(t, max_retries=2, backoff_base=1.0)
    with caplog.at_level(logging.WARNING), pytest.raises(RequestFailed) as e:
        c.get("x", {"crtfc_key": SECRET})
    assert e.value.attempts == 3 and e.value.status == 500
    assert len(sleeps) == 2 and 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0   # full jitter 상한 1, 2초
    assert SECRET not in str(e.value) and SECRET not in caplog.text and "TimeoutError" in caplog.text


def test_other_4xx_fails_immediately():
    t = FakeTransport((404, {}, b""), (200, {}, b"never"))
    c, sleeps = client(t)
    with pytest.raises(RequestFailed) as e:
        c.get("x", {"crtfc_key": SECRET})
    assert e.value.status == 404 and e.value.attempts == 1 and sleeps == [] and len(t.calls) == 1
    assert SECRET not in str(e.value)


def test_too_long_retry_after_stops_instead_of_waiting():
    t = FakeTransport((429, {"retry-after": "3600"}, b""))
    c, sleeps = client(t, max_retry_after=300)
    with pytest.raises(RequestFailed, match="Retry-After"):
        c.get("x", {})
    assert sleeps == []


def test_parse_retry_after():
    assert parse_retry_after("5", NOW) == 5.0
    assert parse_retry_after("Mon, 09 Nov 2026 00:00:30 GMT", NOW) == 30.0
    assert parse_retry_after("Sun, 08 Nov 2026 00:00:00 GMT", NOW) == 0.0    # 지난 시각이면 0
    assert parse_retry_after("soon", NOW) is None and parse_retry_after(None, NOW) is None


def test_budget_stops_before_provider_limit():
    t = FakeTransport(*[(200, {}, b"ok")] * 3)
    c, _ = client(t, budget=CallBudget(2))
    c.get("x", {}), c.get("x", {})
    with pytest.raises(BudgetExhausted):
        c.get("x", {})
    assert len(t.calls) == 2


def test_token_bucket_waits_for_tokens():
    clock = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s

    b = TokenBucket(rate_per_sec=2, capacity=1, clock=lambda: clock[0], sleep=sleep)
    for _ in range(3):
        b.acquire()
    assert sum(slept) == pytest.approx(1.0)          # 첫 토큰은 바로, 다음 둘은 0.5초씩
    clock[0] += 10                                    # 오래 쉬어도 capacity 이상 쌓이지 않는다
    b.acquire()
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_redact_is_case_insensitive():
    assert redact({"CRTFC_KEY": "a", "ServiceKey": "b", "corp_code": "c"}) == {"CRTFC_KEY": "***", "ServiceKey": "***", "corp_code": "c"}


def test_pagination_and_schema_checks():
    check_pagination(expected_rows=250, got_rows=250, expected_pages=3, got_pages=3)
    with pytest.raises(IncompletePagination):
        check_pagination(expected_rows=250, got_rows=200, expected_pages=3, got_pages=2)
    rows = [{"rcept_no": "1", "corp_code": "a", "new_field": 1}, {"rcept_no": "2"}]
    s = check_schema(rows, required=["rcept_no", "corp_code"], known=["rm"])
    assert s.missing == ("corp_code",) and s.unexpected == ("new_field",)
    with pytest.raises(SchemaChanged, match="corp_code"):
        s.raise_if_missing()
    assert check_schema([], ["rcept_no"]).missing == ()
