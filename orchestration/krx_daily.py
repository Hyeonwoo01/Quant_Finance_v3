"""한국 주식 일별 시세 수집. 비공개 저장소에만 저장한다.

공급원 (--source):
- fsc: 금융위원회 주식시세정보 (공공데이터포털, 원천: 한국거래소). 계약상 저장만 허용 (학습 불가).
- krx: 한국거래소 KRX Open API 일별매매정보. 비상업 연구용 저장·학습 허용, 인증키 이용기간(KRX_KEY_VALID_UNTIL) 안에서만.

- 계약상 제3자 제공·재배포가 금지된 데이터다. --data-dir이 비공개 저장소(PRIVATE_STORE 표식)가 아니면 저장 단계가
  PermissionDenied로 멈추고 아무것도 쓰지 않는다 (storage/visibility.py).
- 이 코드는 공개 저장소의 Actions에서 돌고 그 로그는 공개다. 시세 값은 출력하지 않는다 (건수·필드 이름만).
- 기준일(T)의 값은 다음 영업일 13:00 KST 이후에 온다. 매일 최근 며칠을 다시 훑어 늦게 온 날과 바뀐 값을 받는다.
  아직 안 온 날은 경고만 남기고 다음 실행이 다시 묻는다.

사용: python -m orchestration.krx_daily --data-dir private_data [--lookback-sessions 5]
      python -m orchestration.krx_daily --data-dir private_data --start 2024-01-02 --end 2024-12-30   (과거 채우기)
      python -m orchestration.krx_daily --check | --probe                                          (저장 없음)
      python -m orchestration.krx_daily --source krx ...                                           (KRX Open API)
인증키는 환경변수(fsc: DATA_GO_KR_KEY, krx: KRX_API_KEY)에서만 읽고 출력하지 않는다.
"""
import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from dataclasses import dataclass
from typing import Callable

from collectors import krx_openapi
from collectors.base_client import BudgetExhausted, CallBudget, CollectorError
from collectors.fsc_krx import (PRICE_PATH, PROVIDER_READY, SERIES_PRICE, VALUE_FIELDS, FscClient, FscError,
                                price_records, schema_problems)
from collectors.krx_openapi import SERIES_KRX_BASE, SERIES_KRX_PRICE, KrxApiError, KrxOpenApiClient
from core.calendar_align import KR_VENUE, calendar
from core.data_contracts import ContractRegistry, PermissionDenied
from core.repro import code_version
from core.source_registry import FSC_KRX_STOCK_PRICE, KRX_OPENAPI_STOCK, default_registry
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore, by_period_day
from storage.raw_store import RawStore
from storage.visibility import PRIVATE_MARKER, is_private_store

KST = ZoneInfo("Asia/Seoul")
PRICE_SERIES = frozenset({SERIES_PRICE, SERIES_KRX_PRICE, SERIES_KRX_BASE})     # Parquet로 저장하는 일별 시세 시리즈
PUBLICATION_LOG = Path("state") / "krx_publication_log.jsonl"   # KRX 자료가 언제부터 있었는지 관측한 기록
MARKET_DONE = time(15, 45)       # 정규장 마감(15:30) 뒤 여유
MAX_DAYS = 260                   # 한 번 실행에 받는 기준일 수 상한 (과거 채우기를 여러 번에 나눔)


def published_sessions(now_utc: datetime, n: int) -> list[date]:
    """지금 시점에 공급자가 이미 줬어야 하는 기준일 가운데 최근 n개 (오래된 것부터).
    기준일 T는 다음 세션 13:00 KST 이후에 온다 → 마지막 대상은 '13시가 지난 가장 최근 세션'의 바로 앞 세션."""
    return sessions_between(None, last_published(now_utc))[-n:] if n > 0 else []


def last_published(now_utc: datetime) -> date:
    cal = calendar(KR_VENUE)
    local = now_utc.astimezone(KST)
    day = pd.Timestamp(local.date())
    ready = cal.date_to_session(day, direction="previous")          # 오늘 또는 그 전 마지막 세션
    if ready == day and local.time() < PROVIDER_READY:              # 오늘이 세션이어도 13시 전이면 아직 갱신 전
        ready = cal.previous_session(ready)
    return cal.previous_session(ready).date()


def last_closed_session(now_utc: datetime) -> date:
    """장이 끝난(15:45 KST 이후) 가장 최근 KRX 세션."""
    cal = calendar(KR_VENUE)
    local = now_utc.astimezone(KST)
    day = pd.Timestamp(local.date())
    s = cal.date_to_session(day, direction="previous")
    if s == day and local.time() < MARKET_DONE:
        s = cal.previous_session(s)
    return s.date()


def sessions_between(start: Optional[date], end: date) -> list[date]:
    cal = calendar(KR_VENUE)
    start = start or (end - timedelta(days=60))
    return [s.date() for s in cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


@dataclass
class PriceSource:
    """공급원마다 다른 것: 계약·시리즈·클라이언트·응답 → 레코드 변환·필수 필드 검사·오류 종류."""
    name: str
    client: object
    contract: object
    series_id: str
    to_records: Callable
    schema: Callable
    error_cls: type
    extra: tuple = ()          # 같은 응답에서 함께 만드는 시리즈 ((series_id, to_records), ...)


def fsc_source(client: FscClient) -> PriceSource:
    return PriceSource("fsc", client, FSC_KRX_STOCK_PRICE, SERIES_PRICE, price_records, schema_problems, FscError)


def krx_source(client: KrxOpenApiClient) -> PriceSource:
    return PriceSource("krx", client, KRX_OPENAPI_STOCK, SERIES_KRX_PRICE, krx_openapi.price_records,
                       krx_openapi.schema_problems, KrxApiError,
                       extra=((SERIES_KRX_BASE, krx_openapi.base_records),))


class KrxRun:
    def __init__(self, source, data_dir, registry: Optional[ContractRegistry] = None, *,
                 now=lambda: datetime.now(timezone.utc), version: Optional[str] = None):
        self.source = source if isinstance(source, PriceSource) else fsc_source(source)
        self.client, self.data_dir = self.source.client, Path(data_dir)
        self.registry = registry or default_registry()
        self.now, self.version = now, version or code_version()
        self.raw = RawStore(self.data_dir, self.registry)
        # 일별 시세는 하루 수천 행이라 Parquet로 쓴다 (JSON Lines 대비 약 1/16). 남은 JSON Lines는 처음 쓸 때 옮겨진다
        series = [self.source.series_id, *(sid for sid, _ in self.source.extra)]
        self.pit = PITFileStore(self.data_dir, self.registry, partitions={sid: by_period_day for sid in series},
                                parquet_series=PRICE_SERIES)

    def _day_file(self, day: date) -> Optional[Path]:
        return self.pit._existing(self.source.series_id, f"{day:%Y-%m-%d}")

    def has_day(self, day: date) -> bool:
        return self._day_file(day) is not None

    def migrate(self) -> dict:
        """이 공급원 시리즈의 JSON Lines 파일을 모두 Parquet로 옮긴다 (비공개 저장소에서만)."""
        if not is_private_store(self.data_dir):
            raise PermissionDenied(f"{self.data_dir}: 비공개 저장소가 아님")
        return self.pit.migrate_to_parquet(self.source.series_id)

    def collect_day(self, day: date, m: BatchManifest) -> None:
        src = self.source
        pages = self.client.prices(day)
        if not any(p.rows for p in pages):
            m.count("not_yet")
            m.warnings.append(f"{day}: 아직 자료 없음 (다음 실행에서 다시 물음)")
            return
        missing, unexpected = src.schema([r for p in pages if p.rows for r in p.rows])
        if missing:
            m.errors.append(f"{day}: 필수 필드 없음 {list(missing)}")
            return
        if unexpected:
            m.warnings.append(f"{day}: 새 필드 {list(unexpected)}")
        store = self.pit.load(parts=[(sid, f"{day:%Y-%m-%d}") for sid in (src.series_id, *(x for x, _ in src.extra))])
        for p in pages:                               # 페이지마다 그 페이지의 원본 해시로 레코드를 만든다
            ref = self.raw.put(contract_id=src.contract.contract_id, source_id=src.contract.source_id,
                               response=p.response, request_id=m.run_id)
            m.requests.append({"basDt": f"{day:%Y%m%d}", "page_no": p.page_no, "params": p.response.params,
                               "http_status": p.response.status, "attempts": p.response.attempts,
                               "sha256": ref.sha256, "rows": len(p.rows), "total": p.total})
            recs, counts = src.to_records(p.rows, day, contract_id=src.contract.contract_id, raw_sha=ref.sha256,
                                          ingested_at_utc=self.now(), latest=store.latest)
            res = self.pit.append(recs, store=store)
            m.count("added", res.added)
            for k in ("rows", "unchanged", "revised", "bad_rows", "duplicates"):
                if counts[k]:
                    m.count(k, counts[k])
            if counts["bad_rows"]:
                m.warnings.append(f"{day}: 해석 못 한 행 {counts['bad_rows']}개")
            if res.conflicts:
                m.warnings.append(f"{day}: 같은 revision_id인데 내용이 다름 {len(res.conflicts)}건")
            for sid, to_records in src.extra:
                self._append_extra(sid, to_records, p.rows, day, ref.sha256, self.now(), store, m)
        m.count("days")

    def _append_extra(self, sid, to_records, rows, day, raw_sha, ingested_at, store, m) -> None:
        recs, counts = to_records(rows, day, contract_id=self.source.contract.contract_id, raw_sha=raw_sha,
                                  ingested_at_utc=ingested_at, latest=store.latest)
        res = self.pit.append(recs, store=store)
        m.count(f"{sid}_added", res.added)
        if counts["bad_rows"]:
            m.warnings.append(f"{day}: {sid} 해석 못 한 행 {counts['bad_rows']}개")
        if res.conflicts:
            m.warnings.append(f"{day}: {sid} 같은 revision_id인데 내용이 다름 {len(res.conflicts)}건")

    def derive_from_raw(self) -> BatchManifest:
        """저장된 원본 응답으로 함께 만드는 시리즈(extra)를 다시 만든다 (호출 없음). 나중에 추가한 시리즈의 과거분을
        채울 때 쓴다. 받은 시각은 원본을 실제로 받은 시각을 그대로 쓰므로 가용 시각도 그때 기준이다."""
        started = self.now()
        m = BatchManifest.start(run_id=f"{started:%Y%m%dT%H%M%SZ}-derive", contract=self.source.contract,
                                code_version=self.version, now=started,
                                target={"source": self.source.name, "derive": [sid for sid, _ in self.source.extra]})
        if not is_private_store(self.data_dir):
            m.errors.append(f"{self.data_dir}: 비공개 저장소 표식({PRIVATE_MARKER})이 없음")
            m.finish(self.now())
            return m
        apis = {v: k for k, v in krx_openapi.ALL_MARKETS.items()}
        by_day: dict[str, list] = {}
        for e in self.raw.index_entries(self.source.contract.source_id):
            api = (e.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            day = (e.get("params") or {}).get(krx_openapi.DATE_PARAM)
            if e.get("http_status") == 200 and api in apis and day:
                by_day.setdefault(day, []).append(e)
        for day_s in sorted(by_day):
            day = datetime.strptime(day_s, "%Y%m%d").date()
            store = self.pit.load(parts=[(sid, f"{day:%Y-%m-%d}") for sid, _ in self.source.extra])
            for e in sorted(by_day[day_s], key=lambda x: x["received_at_utc"]):    # 받은 순서대로 (정정 이력 유지)
                try:
                    rows = krx_openapi.parse_rows(self.raw.get(self.source.contract.source_id, e["sha256"]))
                except (KrxApiError, ValueError, OSError) as err:
                    m.errors.append(f"{day}: 원본 {e['sha256'][:12]} 읽기 실패 {type(err).__name__}")
                    continue
                if not rows:
                    continue
                got = datetime.fromisoformat(e["received_at_utc"])
                for sid, to_records in self.source.extra:
                    self._append_extra(sid, to_records, rows, day, e["sha256"], got, store, m)
            m.count("days")
        m.finish(self.now())
        m.write(self.data_dir)
        return m

    def observe_publication(self, day: date) -> dict:
        """기준일 자료가 지금 있는지 묻고 (시장마다 한 번) 비공개 저장소에 관측 기록을 남긴다. 저장은 하지 않는다.
        '있음' 관측은 그 시각까지 공개됐다는 증거라, 쌓이면 보수적인 가용 시각 규칙(다음 세션 13:00)을 앞당길 근거가 된다."""
        checked = self.now()
        rows = {}
        for market in self.client.markets:
            try:
                rows[market] = len(self.client.market_day(market, day).rows)
            except KrxApiError as e:
                rows[market] = f"실패({e.code})"
        rec = {"bas_dd": day.isoformat(), "checked_at_utc": checked.isoformat(),
               "checked_at_kst": checked.astimezone(KST).isoformat(), "rows": rows,
               "available": all(isinstance(v, int) and v > 0 for v in rows.values())}
        if is_private_store(self.data_dir):
            p = self.data_dir / PUBLICATION_LOG
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec

    def redo(self, days: list[date], reason: str) -> BatchManifest:
        """잘못된 규칙으로 저장된 날을 다시 받는다 (예: 캘린더에 빠진 휴장일 때문에 가용 시각이 한 세션 이르게
        기록된 날). 그날 파일을 지우고 처음부터 다시 받는다. 지운 사실과 이유는 수집 기록에 남고, 이전 내용은
        비공개 저장소의 git 이력에 남는다. 아직 어떤 분석에도 쓰지 않은 데이터를 고칠 때만 쓴다."""
        removed = []
        if is_private_store(self.data_dir):
            for day in days:
                f = self._day_file(day)
                if f is not None:
                    f.unlink()
                    removed.append(str(day))
        m = self.run(days)
        if not is_private_store(self.data_dir):          # 공개 저장소에는 아무것도 쓰지 않는다
            return m
        m.target.update({"redo": True, "redo_reason": reason, "removed_days": removed})
        m.warnings.insert(0, f"다시 받음 ({reason}): 지운 날 {removed}")
        m.write(self.data_dir)                           # 같은 run_id라 run()이 쓴 기록을 덮어 이유를 남긴다
        return m

    def run(self, days: list[date], *, skip_existing: bool = False) -> BatchManifest:
        started = self.now()
        m = BatchManifest.start(run_id=f"{started:%Y%m%dT%H%M%SZ}", contract=self.source.contract,
                                code_version=self.version, now=started,
                                target={"first": str(days[0]) if days else None, "last": str(days[-1]) if days else None,
                                        "days": len(days), "skip_existing": skip_existing,
                                        "source": self.source.name})
        if not is_private_store(self.data_dir):      # 한 줄도 받기 전에 확인 (저장 단계도 다시 막는다)
            m.errors.append(f"{self.data_dir}: 비공개 저장소 표식({PRIVATE_MARKER})이 없음 — 재배포 금지 데이터라 받지 않음")
            m.finish(self.now())
            return m                                 # 공개 저장소에는 수집 기록도 남기지 않는다
        for day in days:
            if skip_existing and self.has_day(day):
                m.count("skipped_existing")
                continue
            try:
                self.collect_day(day, m)
            except BudgetExhausted as e:
                m.warnings.append(f"{day}: {e} — 나머지는 다음 실행으로")
                break
            except self.source.error_cls as e:
                m.errors.append(f"{day}: {e}")
                if getattr(e, "auth", False):
                    break
            except PermissionDenied:
                raise
            except CollectorError as e:
                m.errors.append(f"{day}: {type(e).__name__}: {e}"[:300])
        m.finish(self.now())
        m.write(self.data_dir)
        return m


def check(client: FscClient, day: date) -> str:
    """저장 없이 인증키·활용신청·연결 확인 (한 건만 요청). 값은 출력하지 않는다."""
    p = client._get(PRICE_PATH, {"basDt": f"{day:%Y%m%d}", "numOfRows": 1, "pageNo": 1})
    return f"주식시세정보: 정상 ({day} 기준 {p.total}건)"


def probe(client: FscClient, day: date) -> list[str]:
    """응답 구조 확인 (저장 없음). 시세 값은 출력하지 않고 건수·필드 이름·형식만 본다."""
    pages = client.prices(day)
    rows = [r for p in pages for r in p.rows]
    lines = [f"{day}: 페이지 {len(pages)}개, 행 {len(rows)}개, 총계 {pages[0].total}건"]
    if not rows:
        return lines
    missing, unexpected = schema_problems(rows)
    lines.append(f"필드: {sorted(rows[0])}")
    lines.append(f"빠진 필수 필드 {list(missing)}, 처음 보는 필드 {list(unexpected)}")
    lines.append(f"시장 구분: {dict(Counter(r.get('mrktCtg') for r in rows))}")
    lines.append(f"ISIN 중복 {len(rows) - len({r.get('isinCd') for r in rows})}건, "
                 f"단축코드 길이 {dict(Counter(len(str(r.get('srtnCd', ''))) for r in rows))}, "
                 f"ISIN 앞 두 글자 {dict(Counter(str(r.get('isinCd', ''))[:2] for r in rows).most_common(5))}")
    not_int = Counter()
    for r in rows:
        for f in VALUE_FIELDS:
            try:
                int(str(r.get(f, "")).replace(",", "").strip())
            except ValueError:
                not_int[f] += 1
    lines.append(f"정수가 아닌 값 {dict(not_int)}, 거래량 0인 종목 {sum(1 for r in rows if str(r.get('trqu')).strip() == '0')}개")
    return lines


def krx_check(client: KrxOpenApiClient, day: date) -> str:
    """저장 없이 인증키·이용신청·연결 확인 (시장마다 한 번). 값은 출력하지 않는다."""
    parts = []
    for market in client.markets:
        try:
            parts.append(f"{market} {len(client.market_day(market, day).rows)}건")
        except KrxApiError as e:
            parts.append(f"{market} 실패({e.code})")
    return f"KRX Open API ({day}): " + ", ".join(parts)


def krx_probe(client: KrxOpenApiClient, day: date) -> list[str]:
    """응답 구조 확인 (저장 없음). 필드 이름·건수·형식만 보고 시세 값은 출력하지 않는다.
    가정한 주소·파라미터·필드 이름(collectors/krx_openapi.py)이 맞는지 여기서 확인한다."""
    lines = [f"주소 {krx_openapi.BASE_URL}/<API ID>, 기준일 파라미터 {krx_openapi.DATE_PARAM}, 행 목록 키 {krx_openapi.ROWS_KEY}"]
    rows = []
    for market in krx_openapi.ALL_MARKETS:          # 신청하지 않은 시장도 물어 승인 상태를 보여 준다
        try:
            page = client.market_day(market, day)
            rows += page.rows
            lines.append(f"{market}: 행 {len(page.rows)}개")
        except KrxApiError as e:
            lines.append(f"{market}: 실패 — {e}")
    if not rows:
        return lines
    missing, unexpected = krx_openapi.schema_problems(rows)
    lines.append(f"필드: {sorted(k for k in rows[0] if k != '_market')}")
    lines.append(f"빠진 필수 필드 {list(missing)}, 처음 보는 필드 {list(unexpected)}")
    lines.append(f"시장 이름: {dict(Counter(r.get('MKT_NM') for r in rows))}, "
                 f"종목코드 길이: {dict(Counter(len(str(r.get('ISU_CD', ''))) for r in rows))}, "
                 f"종목코드 중복 {len(rows) - len({r.get('ISU_CD') for r in rows})}건")
    not_int = Counter()
    for r in rows:
        for f in krx_openapi.FIELDS:
            try:
                float(str(r.get(f, "")).replace(",", "").strip())
            except ValueError:
                not_int[f] += 1
    lines.append(f"숫자로 읽히지 않는 값 {dict(not_int)}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=["fsc", "krx"], default=os.environ.get("QF_PRICE_SOURCE", "fsc"))
    ap.add_argument("--data-dir", default=os.environ.get("QF_PRIVATE_DATA_DIR", "private_data"))
    ap.add_argument("--lookback-sessions", type=int, default=5)
    ap.add_argument("--start", help="과거 채우기 시작 기준일 (YYYY-MM-DD). 이미 받은 날은 건너뛴다")
    ap.add_argument("--end", help="과거 채우기 끝 기준일 (기본: 공급자가 이미 준 마지막 기준일)")
    ap.add_argument("--max-calls", type=int, default=2000, help="이번 실행의 호출 상한 (개발계정 하루 10,000건)")
    ap.add_argument("--redo", help="쉼표로 구분한 기준일들을 지우고 다시 받기 (예: 2026-06-02,2026-07-16)")
    ap.add_argument("--redo-reason", default="", help="다시 받는 이유 (수집 기록에 남김)")
    ap.add_argument("--migrate-parquet", action="store_true", help="이 공급원의 저장된 JSON Lines를 Parquet로 옮기기 (호출 없음)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--derive-from-raw", action="store_true",
                    help="krx: 저장된 원본 응답으로 기준가 시리즈를 다시 만들기 (호출 없음)")
    ap.add_argument("--observe-publication", action="store_true",
                    help="krx: 장이 끝난 가장 최근 세션의 자료가 지금 있는지 관측해 기록 (저장 없음)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key_env = "DATA_GO_KR_KEY" if args.source == "fsc" else "KRX_API_KEY"
    key = os.environ.get(key_env, "").strip()
    if not key:
        print(f"{key_env} 환경변수가 없음 (GitHub Actions에서는 레포 Secrets에 등록)", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    last = last_published(now)
    if args.source == "fsc":
        client = FscClient(key, budget=CallBudget(args.max_calls))
        source = fsc_source(client)
    else:
        markets = tuple(m.strip().upper() for m in os.environ.get("KRX_MARKETS", "").split(",") if m.strip()) or None
        client = KrxOpenApiClient(key, budget=CallBudget(args.max_calls), markets=markets)
        source = krx_source(client)
    try:
        if args.migrate_parquet:
            res = KrxRun(source, args.data_dir).migrate()
            print(f"Parquet로 옮김 [{args.source}]: 파일 {res['files']}개, 레코드 {res['records']:,}건")
            return 0
        if args.check:
            print(check(client, last) if args.source == "fsc" else krx_check(client, last))
            return 0
        if args.probe:
            print("\n".join(probe(client, last) if args.source == "fsc" else krx_probe(client, last)))
            return 0
        if args.source == "krx":                 # 약관 제11조 ③: 이용기간 밖에서는 받지도 쓰지도 않는다
            valid_until = krx_openapi.parse_valid_until(os.environ.get("KRX_KEY_VALID_UNTIL"))
            krx_openapi.check_license(valid_until, now.astimezone(KST).date())
            if is_private_store(args.data_dir):
                krx_openapi.record_license(args.data_dir, valid_until, now)
            if args.derive_from_raw:
                m = KrxRun(source, args.data_dir).derive_from_raw()
                print(f"원본에서 다시 만듦 [krx]: {m.status} {dict(m.counts)}")
                for w in m.errors[:5]:
                    print("오류:", w)
                return 0 if m.status != "failed" else 1
            if args.observe_publication:
                day = last_closed_session(now)
                rec = KrxRun(source, args.data_dir).observe_publication(day)
                print(f"공개 관측: 기준일 {day}, {rec['checked_at_kst'][:16]} KST, 건수 {rec['rows']}, 있음={rec['available']}")
                return 0
        elif args.derive_from_raw or args.observe_publication:
            print("--derive-from-raw·--observe-publication은 --source krx에서만", file=sys.stderr)
            return 2
        if args.redo:
            if not args.redo_reason:
                print("--redo에는 --redo-reason이 필요", file=sys.stderr)
                return 2
            days = [date.fromisoformat(d.strip()) for d in args.redo.split(",") if d.strip()]
            m = KrxRun(source, args.data_dir).redo(days, args.redo_reason)
        elif args.start:
            end = min(date.fromisoformat(args.end), last) if args.end else last
            days = sessions_between(date.fromisoformat(args.start), end)[:MAX_DAYS]
            skip = True
        else:
            days, skip = published_sessions(now, args.lookback_sessions), False
        if not args.redo:
            m = KrxRun(source, args.data_dir).run(days, skip_existing=skip)
    except (CollectorError, PermissionDenied) as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    print(f"일별 시세 [{args.source}]: {m.status} {m.counts} (기준일 {m.target['first']} ~ {m.target['last']})")
    for w in m.warnings[:20]:
        print(f"  경고: {w}")
    for e in m.errors[:20]:
        print(f"  오류: {e}")
    print(f"호출 {client.budget.used}회 / 상한 {client.budget.max_calls}회")
    return 1 if m.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
