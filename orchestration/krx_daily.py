"""한국 주식 일별 시세 수집 (금융위원회 주식시세정보, 원천: 한국거래소). 비공개 저장소에만 저장한다.

- 계약상 제3자 제공·재배포가 금지된 데이터다. --data-dir이 비공개 저장소(PRIVATE_STORE 표식)가 아니면 저장 단계가
  PermissionDenied로 멈추고 아무것도 쓰지 않는다 (storage/visibility.py).
- 이 코드는 공개 저장소의 Actions에서 돌고 그 로그는 공개다. 시세 값은 출력하지 않는다 (건수·필드 이름만).
- 기준일(T)의 값은 다음 영업일 13:00 KST 이후에 온다. 매일 최근 며칠을 다시 훑어 늦게 온 날과 바뀐 값을 받는다.
  아직 안 온 날은 경고만 남기고 다음 실행이 다시 묻는다.

사용: python -m orchestration.krx_daily --data-dir private_data [--lookback-sessions 5]
      python -m orchestration.krx_daily --data-dir private_data --start 2024-01-02 --end 2024-12-30   (과거 채우기)
      python -m orchestration.krx_daily --check | --probe                                          (저장 없음)
인증키는 환경변수 DATA_GO_KR_KEY에서만 읽고 출력하지 않는다.
"""
import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from collectors.base_client import BudgetExhausted, CallBudget, CollectorError
from collectors.fsc_krx import (PRICE_PATH, PROVIDER_READY, SERIES_PRICE, VALUE_FIELDS, FscClient, FscError,
                                price_records, schema_problems)
from core.calendar_align import KR_VENUE, calendar
from core.data_contracts import ContractRegistry, PermissionDenied
from core.repro import code_version
from core.source_registry import FSC_KRX_STOCK_PRICE, default_registry
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore, by_period_day
from storage.raw_store import RawStore
from storage.visibility import PRIVATE_MARKER, is_private_store

KST = ZoneInfo("Asia/Seoul")
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


def sessions_between(start: Optional[date], end: date) -> list[date]:
    cal = calendar(KR_VENUE)
    start = start or (end - timedelta(days=60))
    return [s.date() for s in cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


class KrxRun:
    def __init__(self, client: FscClient, data_dir, registry: Optional[ContractRegistry] = None, *,
                 now=lambda: datetime.now(timezone.utc), version: Optional[str] = None):
        self.client, self.data_dir = client, Path(data_dir)
        self.registry = registry or default_registry()
        self.now, self.version = now, version or code_version()
        self.raw = RawStore(self.data_dir, self.registry)
        self.pit = PITFileStore(self.data_dir, self.registry, partitions={SERIES_PRICE: by_period_day})

    def has_day(self, day: date) -> bool:
        return (self.data_dir / "pit_versions" / SERIES_PRICE / f"{day:%Y-%m-%d}.jsonl").exists()

    def collect_day(self, day: date, m: BatchManifest) -> None:
        pages = self.client.prices(day)
        if not any(p.rows for p in pages):
            m.count("not_yet")
            m.warnings.append(f"{day}: 아직 자료 없음 (다음 실행에서 다시 물음)")
            return
        missing, unexpected = schema_problems([r for p in pages for r in p.rows])
        if missing:
            m.errors.append(f"{day}: 필수 필드 없음 {list(missing)}")
            return
        if unexpected:
            m.warnings.append(f"{day}: 새 필드 {list(unexpected)}")
        store = self.pit.load(parts=[(SERIES_PRICE, f"{day:%Y-%m-%d}")])
        for p in pages:                               # 페이지마다 그 페이지의 원본 해시로 레코드를 만든다
            ref = self.raw.put(contract_id=FSC_KRX_STOCK_PRICE.contract_id, source_id=FSC_KRX_STOCK_PRICE.source_id,
                               response=p.response, request_id=m.run_id)
            m.requests.append({"basDt": f"{day:%Y%m%d}", "page_no": p.page_no, "params": p.response.params,
                               "http_status": p.response.status, "attempts": p.response.attempts,
                               "sha256": ref.sha256, "rows": len(p.rows), "total": p.total})
            recs, counts = price_records(p.rows, day, contract_id=FSC_KRX_STOCK_PRICE.contract_id, raw_sha=ref.sha256,
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
        m.count("days")

    def run(self, days: list[date], *, skip_existing: bool = False) -> BatchManifest:
        started = self.now()
        m = BatchManifest.start(run_id=f"{started:%Y%m%dT%H%M%SZ}", contract=FSC_KRX_STOCK_PRICE,
                                code_version=self.version, now=started,
                                target={"first": str(days[0]) if days else None, "last": str(days[-1]) if days else None,
                                        "days": len(days), "skip_existing": skip_existing})
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
            except FscError as e:
                m.errors.append(f"{day}: {e}")
                if e.auth:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=os.environ.get("QF_PRIVATE_DATA_DIR", "private_data"))
    ap.add_argument("--lookback-sessions", type=int, default=5)
    ap.add_argument("--start", help="과거 채우기 시작 기준일 (YYYY-MM-DD). 이미 받은 날은 건너뛴다")
    ap.add_argument("--end", help="과거 채우기 끝 기준일 (기본: 공급자가 이미 준 마지막 기준일)")
    ap.add_argument("--max-calls", type=int, default=2000, help="이번 실행의 호출 상한 (개발계정 하루 10,000건)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        print("DATA_GO_KR_KEY 환경변수가 없음 (GitHub Actions에서는 레포 Secrets에 등록)", file=sys.stderr)
        return 2
    client = FscClient(key, budget=CallBudget(args.max_calls))
    now = datetime.now(timezone.utc)
    last = last_published(now)
    try:
        if args.check:
            print(check(client, last))
            return 0
        if args.probe:
            print("\n".join(probe(client, last)))
            return 0
        if args.start:
            end = min(date.fromisoformat(args.end), last) if args.end else last
            days = sessions_between(date.fromisoformat(args.start), end)[:MAX_DAYS]
            skip = True
        else:
            days, skip = published_sessions(now, args.lookback_sessions), False
        m = KrxRun(client, args.data_dir).run(days, skip_existing=skip)
    except (CollectorError, PermissionDenied) as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    print(f"일별 시세: {m.status} {m.counts} (기준일 {m.target['first']} ~ {m.target['last']})")
    for w in m.warnings[:20]:
        print(f"  경고: {w}")
    for e in m.errors[:20]:
        print(f"  오류: {e}")
    print(f"호출 {client.budget.used}회 / 상한 {client.budget.max_calls}회")
    return 1 if m.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
