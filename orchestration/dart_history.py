"""OpenDART 과거 재무 채우기 실행 (2015년~). 공개 data 브랜치에 Parquet로 저장한다 (OpenDART는 재배포 허용).

    python -m orchestration.dart_history --data-dir data [--start 2015-01-01] [--end 2026-11-15] [--from-index]

1. 정기공시 목록(list.json, pblntf_ty=A)을 3개월 창으로 모두 훑는다 → 시리즈 dart_periodic_index
2. 12월 결산 표준 보고서의 (사업연도, 보고서 코드)마다 회사 100개씩 다중회사 주요계정 → 시리즈 dart_fin_hist
   가용 시각 규칙은 collectors/opendart_history.py (마지막 재무 변경일 다음 세션 08:30).
   API가 2015 사업연도부터라, 그 전 연도의 늦은 제출은 목록에만 남기고 값은 받지 않는다.
목록 훑기가 빠짐없이 끝나면 state/dart_history_scan.json에 기간을 남긴다. --from-index는 그 기간 안에서만
저장된 목록을 다시 써 값 받기만 한다 (훑기에 두 시간 가까이 걸려서).
"""
import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from collectors.base_client import BudgetExhausted, CallBudget, CollectorError
from collectors.opendart import OpenDartClient, OpenDartError
from collectors.opendart_history import (SERIES_FIN_HIST, SERIES_REPORT_INDEX, fin_records, fin_values, index_records,
                                         last_change, period_end_of, report_rows, rows_from_records, windows)
from core.repro import code_version
from core.source_registry import OPENDART_FINANCIALS, OPENDART_LIST, default_registry
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore, by_period_month
from storage.raw_store import RawStore

KST = ZoneInfo("Asia/Seoul")
HISTORY_SERIES = (SERIES_REPORT_INDEX, SERIES_FIN_HIST)


RETRIES, RETRY_WAIT = 3, 60.0          # JSON이 아닌 응답(점검 페이지 등)·연결 오류는 기다렸다 다시 묻는다
FIRST_VALUE_YEAR = 2015                # 다중회사 주요계정 API가 주는 첫 사업연도 (그 전 연도의 늦은 제출은 목록에만 남긴다)
SCAN_STATE = "state/dart_history_scan.json"


class HistoryRun:
    def __init__(self, client, data_dir, *, now=lambda: datetime.now(timezone.utc), version=None, log=print,
                 sleep=None, retry_wait: float = RETRY_WAIT):
        import time as _time
        self.client, self.data_dir, self.now, self.log = client, Path(data_dir), now, log
        self.sleep, self.retry_wait = sleep or _time.sleep, retry_wait
        self.registry = default_registry()
        self.version = version or code_version()
        self.raw = RawStore(self.data_dir, self.registry)
        self.pit = PITFileStore(self.data_dir, self.registry, partitions={s: by_period_month for s in HISTORY_SERIES},
                                parquet_series=HISTORY_SERIES)

    def _retry(self, what: str, fn, m: BatchManifest):
        """일시 오류는 다시 시도하고, 그래도 안 되면 None (그 단위만 건너뛰고 경고). 인증·한도 오류는 그대로 올린다."""
        for i in range(RETRIES):
            try:
                return fn()
            except OpenDartError as e:
                if e.status not in ("parse", "800", "900"):          # 인증키·한도 등은 다시 물어도 소용없다
                    raise
                err = e
            except CollectorError as e:
                if isinstance(e, BudgetExhausted):
                    raise
                err = e
            m.count("retries")
            self.sleep(self.retry_wait * (i + 1))
        m.warnings.append(f"{what}: {type(err).__name__} {err} — {RETRIES}번 시도 뒤 건너뜀 (다음 실행에서 다시)")
        m.count("skipped_units")
        return None

    def scan(self, start: date, end: date, m: BatchManifest) -> list:
        rows, counts = [], Counter()
        for bgn, fin in windows(start, end):
            got_pages = self._retry(f"공시 목록 {bgn}~{fin}",
                                    lambda: self.client.list_filings(bgn, fin, None, pblntf_ty="A"), m)
            if got_pages is None:
                continue
            pages, problem = got_pages
            if problem:
                m.warnings.append(problem)
            for p in pages:
                if not p.rows:
                    continue
                ref = self.raw.put(contract_id=OPENDART_LIST.contract_id, source_id=OPENDART_LIST.source_id,
                                   response=p.response, request_id=m.run_id)
                got, c = report_rows(p.rows)
                counts.update(c)
                recs = index_records(got, contract_id=OPENDART_LIST.contract_id, raw_sha=ref.sha256,
                                     ingested_at_utc=self.now(), latest=lambda k: None)
                res = self.pit.append(recs)
                m.count("index_added", res.added)
                rows += got
            m.count("list_windows")
        for k, v in counts.items():
            m.count(k, v)
        return rows

    def fetch_values(self, rows: list, m: BatchManifest, *, only_missing: bool = False) -> None:
        """only_missing: 이미 저장된 (회사, 연도, 보고서)는 다시 묻지 않는다 (건너뛴 묶음만 채울 때).
        이 경우 이미 있는 값의 정정은 확인하지 않으므로, 정정 반영은 전체 실행으로 한다."""
        changes = last_change(rows)
        keys = {r.key for r in rows}
        by_period: dict[tuple, list] = {}
        for corp, year, code in keys:
            if year < FIRST_VALUE_YEAR:
                m.count("value_skipped_pre_api_year")
                continue
            by_period.setdefault((year, code), []).append(corp)
        stores: dict[int, object] = {}
        for (year, code), corps in sorted(by_period.items()):
            corps = sorted(set(corps))
            period_end = period_end_of(year, code)
            if year not in stores:                                                   # 분할: 관측 시작(1월 1일) 월
                stores = {year: self.pit.load(parts=[(SERIES_FIN_HIST, f"{year}-01")])}
            store = stores[year]
            if only_missing:
                have = [c for c in corps if store.latest((c, SERIES_FIN_HIST, f"fin|{code}", date(year, 1, 1),
                                                          period_end, None)) is not None]
                m.count("value_already_stored", len(have))
                corps = sorted(set(corps) - set(have))
            for i in range(0, len(corps), 100):
                batch = corps[i:i + 100]
                r = self._retry(f"주요계정 {year} {code} {i // 100 + 1}번째 묶음",
                                lambda: self.client.multi_account(batch, year, code), m)
                m.count("value_calls")
                if r is None:
                    continue
                if not r.rows:
                    m.count("value_empty_batches")
                    continue
                ref = self.raw.put(contract_id=OPENDART_FINANCIALS.contract_id, source_id=OPENDART_FINANCIALS.source_id,
                                   response=r.response, request_id=m.run_id)
                vals = fin_values(r.rows)
                m.count("companies_returned", len(vals))
                m.count("companies_missing", len(set(batch) - set(vals)))
                recs, c = fin_records(vals, year, code, period_end, changes, contract_id=OPENDART_FINANCIALS.contract_id,
                                      raw_sha=ref.sha256, ingested_at_utc=self.now(), latest=store.latest)
                res = self.pit.append(recs, store=store)
                m.count("fin_added", res.added)
                for k, v in c.items():
                    if v:
                        m.count(f"fin_{k}", v)
            self.log(f"  {year} {code}: 회사 {len(corps)}개")

    def _state_path(self) -> Path:
        return self.data_dir / SCAN_STATE

    def rows_from_index(self, start: date, end: date, m: BatchManifest) -> Optional[list]:
        """이전 실행이 빠짐없이 끝낸 목록 훑기(상태 파일)가 start부터 덮으면, 저장된 목록에서 정기보고서 행을
        다시 만든다 (끝은 훑은 날까지로 줄인다). start를 덮지 못하면 None (목록을 다시 훑어야 한다)."""
        p = self._state_path()
        if not p.exists():
            return None
        st = json.loads(p.read_text())
        if date.fromisoformat(st["start"]) > start:
            return None
        if date.fromisoformat(st["end"]) < end:                # 훑은 날까지만 쓴다 (그 뒤 접수분은 매일 수집이 맡는다)
            end = date.fromisoformat(st["end"])
            m.warnings.append(f"저장된 공시 목록이 {end}까지라 그날까지만 씀")
        rows = [r for r in rows_from_records(self.pit.iter_records([SERIES_REPORT_INDEX])) if start <= r.rcept_dt <= end]
        m.count("index_rows_reused", len(rows))
        return rows

    def run(self, start: date, end: date, *, from_index: bool = False, only_missing: bool = False) -> BatchManifest:
        started = self.now()
        m = BatchManifest.start(run_id=f"{started:%Y%m%dT%H%M%SZ}-history", contract=OPENDART_FINANCIALS,
                                code_version=self.version, now=started, target={"start": str(start), "end": str(end)})
        try:
            rows = self.rows_from_index(start, end, m) if from_index else None
            if from_index and rows is None:
                m.errors.append("저장된 공시 목록이 이 기간을 빠짐없이 덮는다는 기록이 없음 — from_index 없이 다시 실행")
            else:
                if rows is None:
                    rows = self.scan(start, end, m)
                    if not m.counts.get("skipped_units"):
                        self._state_path().parent.mkdir(parents=True, exist_ok=True)
                        self._state_path().write_text(json.dumps(
                            {"start": str(start), "end": str(end), "completed_at_utc": self.now().isoformat(),
                             "rows": len(rows)}, indent=1) + "\n")
                years = Counter(r.key[1] for r in rows)
                self.log(f"정기보고서 {len(rows):,}건 (연도별 {dict(sorted(years.items()))})")
                if m.counts.get("skipped_units"):
                    # 목록에 빠진 기간이 있으면 그 기간의 [기재정정]을 놓쳐, 정정된 값을 정정 전 날짜로 저장할 수 있다
                    m.errors.append("공시 목록에 빠진 기간이 있어 값 받기를 하지 않음 (가용 시각이 이르게 잡힐 위험) — 다시 실행")
                else:
                    self.fetch_values(rows, m, only_missing=only_missing)
        except BudgetExhausted as e:
            m.warnings.append(f"{e} — 나머지는 다음 실행으로")
        except (OpenDartError, CollectorError) as e:
            m.errors.append(f"{type(e).__name__}: {e}"[:300])
        m.finish(self.now())
        m.write(self.data_dir)
        return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None, help="기본: 어제 (KST)")
    ap.add_argument("--max-calls", type=int, default=9000)
    ap.add_argument("--from-index", action="store_true",
                    help="공시 목록을 다시 훑지 않고, 이전에 빠짐없이 끝낸 훑기의 저장본으로 값만 받는다")
    ap.add_argument("--only-missing", action="store_true", help="이미 저장된 (회사, 연도, 보고서)는 건너뛴다")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("DART_API_KEY 환경변수가 없음", file=sys.stderr)
        return 2
    end = date.fromisoformat(args.end) if args.end else datetime.now(KST).date() - timedelta(days=1)
    client = OpenDartClient(key, budget=CallBudget(args.max_calls))
    m = HistoryRun(client, args.data_dir).run(date.fromisoformat(args.start), end, from_index=args.from_index,
                                         only_missing=args.only_missing)
    print(f"과거 재무 채우기: {m.status} {dict(m.counts)}")
    for w in m.warnings[:10]:
        print("경고:", w)
    for e in m.errors[:10]:
        print("오류:", e)
    print(f"호출 {client.budget.used}회 / 상한 {client.budget.max_calls}회")
    return 0 if m.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
