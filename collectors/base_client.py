"""수집 클라이언트 공통 기능 (가이드 6-3).

- 호출 속도 제한(토큰 버킷)과 일일 호출 예산. 재시도도 호출 한 번으로 센다 (공급자는 요청마다 센다).
- HTTP 429·5xx·네트워크 오류는 지수 백오프 + jitter로 재시도하고, Retry-After를 따른다. 그 밖의 4xx는 바로 실패.
- 인증키 같은 비밀 파라미터는 로그·오류·저장 기록에 남기지 않는다 (redact).
- 페이지 완결성과 응답 스키마 변화를 확인하는 도구.

네트워크 전송은 transport 함수로 분리해 테스트에서 바꿔 끼운다. 공급자별 응답 코드(예: HTTP 200 안의 오류 코드)는
공급자 모듈에서 처리한다.
"""
import email.utils
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

# 이름만으로 비밀로 취급하는 파라미터 (공급자 모듈이 더 추가할 수 있다)
SECRET_PARAM_NAMES = frozenset({"crtfc_key", "api_key", "apikey", "key", "servicekey", "token", "access_token"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDACTED = "***"

Transport = Callable[[str, Mapping[str, str], float], tuple]


class CollectorError(Exception):
    """수집 실패의 공통 부모."""


class BudgetExhausted(CollectorError):
    """그 실행에 배정된 호출 수를 다 썼다. 공급자 한도를 넘기 전에 멈춘다."""


class RequestFailed(CollectorError):
    def __init__(self, message: str, status: Optional[int], attempts: int):
        super().__init__(message)
        self.status, self.attempts = status, attempts


class IncompletePagination(CollectorError):
    """페이지나 건수가 공급자가 알려 준 총계와 맞지 않는다."""


class SchemaChanged(CollectorError):
    """필수 필드가 응답에서 사라졌다."""


def redact(params: Mapping[str, object], secret_names: Iterable[str] = SECRET_PARAM_NAMES) -> dict:
    """비밀 파라미터 값을 가린 사본 (대소문자 무시)."""
    secret = {s.lower() for s in secret_names}
    return {k: (REDACTED if k.lower() in secret else v) for k, v in params.items()}


@dataclass(frozen=True)
class HttpResponse:
    url: str                      # 쿼리 문자열 없는 주소 (비밀이 섞이지 않게)
    params: dict                  # 가린 파라미터
    status: int
    body: bytes
    headers: dict
    attempts: int
    requested_at_utc: datetime
    received_at_utc: datetime

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def urllib_transport(url: str, params: Mapping[str, str], timeout: float):
    """표준 라이브러리 전송. HTTPS_PROXY 같은 환경 설정을 따른다. 반환: (status, headers, body)."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full, headers={"User-Agent": "quant-finance-v3-collector"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:   # 4xx·5xx도 응답으로 돌려 재시도 여부를 위에서 판단
        return e.code, dict(e.headers or {}), (e.read() if e.fp else b"")


class TokenBucket:
    """초당 rate개, 최대 capacity개까지 쌓이는 토큰. 토큰이 없으면 생길 때까지 기다린다."""

    def __init__(self, rate_per_sec: float, capacity: float = 1.0, clock=time.monotonic, sleep=time.sleep):
        if rate_per_sec <= 0 or capacity < 1:
            raise ValueError("rate_per_sec > 0, capacity >= 1 이어야 함")
        self.rate, self.capacity, self.clock, self.sleep = rate_per_sec, capacity, clock, sleep
        self.tokens, self.last = capacity, clock()

    def acquire(self) -> None:
        while True:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            self.sleep((1 - self.tokens) / self.rate)


class CallBudget:
    """한 실행에서 쓸 수 있는 호출 수. 공급자 일일 한도보다 낮게 잡아 여유를 둔다."""

    def __init__(self, max_calls: int):
        if max_calls < 1:
            raise ValueError("max_calls >= 1")
        self.max_calls, self.used = max_calls, 0

    @property
    def remaining(self) -> int:
        return self.max_calls - self.used

    def take(self) -> None:
        if self.used >= self.max_calls:
            raise BudgetExhausted(f"호출 예산 {self.max_calls}회를 모두 씀")
        self.used += 1


def parse_retry_after(value: Optional[str], now: datetime) -> Optional[float]:
    """Retry-After (초 또는 HTTP 날짜) → 기다릴 초. 해석 못 하면 None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - now).total_seconds(), 0.0)


class BaseClient:
    def __init__(self, base_url: str, *, secret_params: Iterable[str] = (), secret_values: Iterable[str] = (),
                 rate: Optional[TokenBucket] = None,
                 budget: Optional[CallBudget] = None, max_retries: int = 4, backoff_base: float = 1.0,
                 backoff_cap: float = 60.0, max_retry_after: float = 300.0, timeout: float = 30.0,
                 transport: Transport = urllib_transport, sleep=time.sleep, rng: Optional[random.Random] = None,
                 now=lambda: datetime.now(timezone.utc)):
        self.base_url = base_url.rstrip("/")
        self.secret_params = SECRET_PARAM_NAMES | {s.lower() for s in secret_params}
        # 주소 경로에 들어가는 키(예: ECOS)처럼 이름이 없는 비밀 값. 주소·오류·로그에서 지운다
        self.secret_values = tuple(v for v in secret_values if v)
        self.rate, self.budget = rate, budget
        self.max_retries, self.backoff_base, self.backoff_cap = max_retries, backoff_base, backoff_cap
        self.max_retry_after, self.timeout = max_retry_after, timeout
        self.transport, self.sleep, self.rng, self.now = transport, sleep, rng or random.Random(), now

    def scrub(self, text: str) -> str:
        for v in self.secret_values:
            text = text.replace(v, REDACTED)
        return text

    def _backoff(self, attempt: int) -> float:
        """full jitter: 0 ~ min(cap, base·2^(attempt−1)) 사이 무작위."""
        return self.rng.uniform(0, min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1)))

    def get(self, path: str, params: Mapping[str, object]) -> HttpResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        send = {k: str(v) for k, v in params.items() if v is not None}
        shown = {k: self.scrub(v) for k, v in redact(send, self.secret_params).items()}
        shown_url = self.scrub(url)
        requested_at = self.now()
        for attempt in range(1, self.max_retries + 2):
            if self.rate:
                self.rate.acquire()
            if self.budget:
                self.budget.take()
            headers: dict = {}
            try:
                status, headers, body = self.transport(url, send, self.timeout)
            except OSError as e:   # 연결 실패·시간 초과 (URLError 포함). 메시지에 주소가 섞일 수 있어 종류만 남긴다
                status, reason = None, f"네트워크 오류 {type(e).__name__}"
            else:
                headers = dict(headers or {})
                if status not in RETRYABLE_STATUS:
                    if status >= 400:
                        raise RequestFailed(f"{shown_url} {shown}: HTTP {status}", status, attempt)
                    return HttpResponse(shown_url, shown, status, body, headers, attempt, requested_at, self.now())
                reason = f"HTTP {status}"
            if attempt > self.max_retries:
                raise RequestFailed(f"{shown_url} {shown}: {reason}, {attempt}회 시도 후 실패", status, attempt)
            ra = parse_retry_after({k.lower(): v for k, v in headers.items()}.get("retry-after"), self.now())
            if ra is not None and ra > self.max_retry_after:
                raise RequestFailed(f"{shown_url} {shown}: Retry-After {ra:.0f}초 — 기다리지 않고 멈춤", status, attempt)
            delay = ra if ra is not None else self._backoff(attempt)
            log.warning("%s %s: %s, %.1f초 뒤 재시도 (%d/%d)", shown_url, shown, reason, delay, attempt, self.max_retries)
            self.sleep(delay)
        raise AssertionError("도달하지 않음")


def check_pagination(*, expected_rows: int, got_rows: int, expected_pages: int, got_pages: int) -> None:
    """공급자가 알려 준 총 건수·총 페이지와 실제로 받은 것이 같아야 완결."""
    if got_pages != expected_pages or got_rows != expected_rows:
        raise IncompletePagination(f"페이지 {got_pages}/{expected_pages}, 건수 {got_rows}/{expected_rows}")


@dataclass(frozen=True)
class SchemaCheck:
    missing: tuple            # 일부 행에서라도 빠진 필수 필드
    unexpected: tuple         # 처음 보는 필드 (경고로 기록)

    def raise_if_missing(self) -> None:
        if self.missing:
            raise SchemaChanged(f"필수 필드 없음: {', '.join(self.missing)}")


def check_schema(rows: Iterable[Mapping], required: Iterable[str], known: Iterable[str] = ()) -> SchemaCheck:
    rows = list(rows)
    required = list(required)
    missing = sorted({f for f in required for r in rows if f not in r})
    seen = set().union(*(r.keys() for r in rows)) if rows else set()
    unexpected = sorted(seen - set(required) - set(known))
    return SchemaCheck(tuple(missing), tuple(unexpected))
