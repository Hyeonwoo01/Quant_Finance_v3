"""OpenDART 매일 수집 (GitHub Actions에서 한국 장 시작 전에 실행).

1) 최근 7일(끝난 날짜만)의 상장사(유가·코스닥) 공시 목록을 받아 원본과 PIT 레코드로 저장한다.
   같은 기간을 매일 다시 훑으므로 늦게 바뀐 비고(정정·철회 표시)도 새 버전으로 남는다.
2) 그중 정기보고서(사업·반기·분기, 최초 제출 또는 [기재정정])는 재무제표 대기열에 넣는다.
   재무제표 API는 공시 직후 바로 채워지지 않을 수 있어, 응답의 접수번호가 그 보고서와 같을 때만 받고 아니면 다음 날 다시 묻는다.
3) 실행마다 수집 기록(manifest)을 남긴다. 호출 예산을 넘기 전에 멈추고, 남은 일은 대기열에 둔다.

사용: python -m orchestration.opendart_daily --data-dir data [--lookback-days 7] [--max-calls 8000] [--check]
인증키는 환경변수 DART_API_KEY에서만 읽고 어디에도 출력하지 않는다.
"""
import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from collectors.base_client import BudgetExhausted, CallBudget, CollectorError, check_schema
from collectors.opendart import (FS_KNOWN, FS_REQUIRED, LIST_REQUIRED, NO_DATA, OK, SERIES_FILINGS, SERIES_FS, ApiResult, FilingRef,
                                 OpenDartClient, OpenDartError, filing_record, fs_candidates, fs_records,
                                 parse_periodic, parse_rcept_dt)
from collectors.opendart_segments import SERIES_SEGMENTS, extract_segments, segment_records
from collectors.opendart_xbrl import fetch_xbrl, parse_xbrl_zip
from core.data_contracts import ContractRegistry
from core.repro import code_version
from core.source_registry import OPENDART_FINANCIALS, OPENDART_LIST, OPENDART_XBRL, default_registry
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore, by_period_month
from storage.raw_store import RawStore

log = logging.getLogger("opendart_daily")
KST = ZoneInfo("Asia/Seoul")
LIST_CORP_CLS = ("Y", "K")       # 유가증권·코스닥 상장사 공시 전체
FS_CORP_CLS = ("Y",)             # 재무제표는 우선 유가증권시장 상장사 (저장 용량 때문, 넓히려면 여기만 바꾼다)
GIVE_UP_DAYS = 14                # 이 기간 동안 재무제표 API에 그 보고서가 안 나오면 대기열에서 빼고 경고
STATE_FILE = Path("state") / "opendart_fnltt.json"
CALLS_PER_FS_ITEM = 6            # 대기열 한 건에 드는 최대 호출 수 (후보 4 + 연결 1 + 여유)
XBRL_STATE_FILE = Path("state") / "opendart_xbrl.json"
XBRL_MAX_ATTEMPTS = 5            # XBRL 파일이 아직 없다는 응답이 이만큼 이어지면 포기하고 경고
XBRL_MINUTES = 25.0              # 실행당 XBRL 단계 시간 예산 (보고서 하나 받는 데 수십 초 걸릴 수 있음)


@dataclass
class Pending:
    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: str
    rm: str
    first_seen_utc: str
    attempts: int = 0
    last_try_utc: Optional[str] = None


@dataclass
class FsState:
    pending: dict = field(default_factory=dict)     # rcept_no → Pending
    done: dict = field(default_factory=dict)        # rcept_no → {corp_code, kind, period_end, bsns_year, reprt_code, fs_divs, done_at_utc}
    gave_up: dict = field(default_factory=dict)     # rcept_no → {..., reason}

    @classmethod
    def load(cls, data_dir: Path) -> "FsState":
        p = data_dir / STATE_FILE
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        st = cls({k: Pending(**v) for k, v in d.get("pending", {}).items()}, d.get("done", {}), d.get("gave_up", {}))
        st.requeue_unchecked_cfs()
        return st

    def requeue_unchecked_cfs(self) -> None:
        """연결재무제표를 묻지 않고 끝낸 항목(비고 '연'으로 판단하던 시기)은 대기열로 되돌린다.
        다시 받으면 별도재무제표는 '변화 없음'으로 건너뛰고 연결재무제표만 새로 들어간다."""
        for rcept_no, d in list(self.done.items()):
            if d.get("cfs_checked") or "CFS" in d.get("fs_divs", []):
                continue
            self.pending[rcept_no] = Pending(rcept_no, d["corp_code"], d.get("corp_name", ""), d["report_nm"],
                                             d.get("rcept_dt", rcept_no[:8]), "", datetime.now(timezone.utc).isoformat())
            del self.done[rcept_no]

    def save(self, data_dir: Path) -> None:
        p = data_dir / STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        d = {"pending": {k: asdict(v) for k, v in sorted(self.pending.items())},
             "done": dict(sorted(self.done.items())), "gave_up": dict(sorted(self.gave_up.items()))}
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def known(self, rcept_no: str) -> bool:
        return rcept_no in self.pending or rcept_no in self.done or rcept_no in self.gave_up


@dataclass
class RunResult:
    list_manifest: BatchManifest
    fs_manifest: Optional[BatchManifest]
    stopped: Optional[str] = None                   # 전체를 멈춘 이유 (키 오류·한도 초과·점검 등)
    xbrl_manifest: Optional[BatchManifest] = None

    @property
    def failed(self) -> bool:
        return self.stopped is not None or self.list_manifest.status == "failed" or \
            any(m is not None and m.status == "failed" for m in (self.fs_manifest, self.xbrl_manifest))


@dataclass
class XbrlState:
    """재무제표를 받은 정기보고서 중 XBRL(부문·지역) 처리 상태."""
    attempts: dict = field(default_factory=dict)    # rcept_no → 시도 횟수
    done: dict = field(default_factory=dict)        # rcept_no → {segment, geography, values, done_at_utc}
    gave_up: dict = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> "XbrlState":
        p = data_dir / XBRL_STATE_FILE
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(d.get("attempts", {}), d.get("done", {}), d.get("gave_up", {}))

    def save(self, data_dir: Path) -> None:
        p = data_dir / XBRL_STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        d = {"attempts": dict(sorted(self.attempts.items())), "done": dict(sorted(self.done.items())),
             "gave_up": dict(sorted(self.gave_up.items()))}
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _request_entry(r: ApiResult, sha: str, **extra) -> dict:
    return {"endpoint": r.endpoint, "params": r.response.params, "http_status": r.response.status,
            "status": r.status, "attempts": r.response.attempts, "sha256": sha, "rows": len(r.rows), **extra}


def _link(rec, store):
    """같은 관측값의 저장된 최신 버전이 있고 그것과 다른 버전이면 이전 버전을 가리키게 한다."""
    prev = store.latest(rec.observation_key)
    if prev is not None and prev.revision_id != rec.revision_id:
        return replace(rec, supersedes_revision_id=prev.revision_id)
    return rec


class DailyRun:
    def __init__(self, client: OpenDartClient, data_dir, registry: Optional[ContractRegistry] = None, *,
                 now=lambda: datetime.now(timezone.utc), version: Optional[str] = None, clock=time.monotonic):
        self.client, self.data_dir, self.clock = client, Path(data_dir), clock
        self.registry = registry or default_registry()
        self.now = now
        self.version = version or code_version()
        self.raw = RawStore(self.data_dir, self.registry)
        self.pit = PITFileStore(self.data_dir, self.registry, partitions={SERIES_FILINGS: by_period_month})

    # ---------- 1) 공시 목록 ----------
    def collect_filings(self, bgn: date, end: date, run_id: str, m: BatchManifest) -> tuple[dict, Optional[str]]:
        rows: dict[str, tuple[dict, str]] = {}
        stopped = None
        for cls in LIST_CORP_CLS:
            try:
                pages, problem = self.client.list_filings(bgn, end, cls)
            except BudgetExhausted as e:
                m.errors.append(f"list {cls}: {e}")
                stopped = "budget"
                break
            except OpenDartError as e:
                m.errors.append(f"list {cls}: {e}")
                if e.stop_all:
                    stopped = f"OpenDART status {e.status}"
                    break
                continue
            except CollectorError as e:
                m.errors.append(f"list {cls}: {e}")
                continue
            for p in pages:
                ref = self.raw.put(contract_id=OPENDART_LIST.contract_id, source_id=OPENDART_LIST.source_id,
                                   response=p.response, request_id=run_id)
                m.requests.append(_request_entry(p, ref.sha256, page_no=p.data.get("page_no")))
                schema = check_schema(p.rows, LIST_REQUIRED)
                if schema.missing:
                    m.errors.append(f"list {cls}: 필수 필드 없음 {schema.missing}")
                    continue
                if schema.unexpected:
                    m.warnings.append(f"list {cls}: 새 필드 {schema.unexpected}")
                for row in p.rows:
                    key = row["rcept_no"].strip()
                    if key in rows:
                        m.count("duplicates")
                    rows[key] = (row, ref.sha256)
            if problem:
                m.warnings.append(problem)

        ingested = self.now()
        recs = []
        for key, (row, sha) in list(rows.items()):
            try:
                recs.append(filing_record(row, contract_id=OPENDART_LIST.contract_id, raw_sha=sha, ingested_at_utc=ingested))
            except (ValueError, AttributeError) as e:      # 접수일 형식 이상 등: 그 건만 건너뛴다
                m.count("parse_errors")
                m.warnings.append(f"{key}: 변환 실패 {e}")
                del rows[key]
        store = self.pit.load_for(recs)
        res = self.pit.append([_link(r, store) for r in recs], store=store)
        m.count("rows", len(rows))
        m.count("added", res.added)
        m.count("unchanged", res.unchanged)
        if res.conflicts:
            m.warnings.append(f"같은 revision_id인데 내용이 다름 (쓰지 않음): {list(res.conflicts)[:20]}")
        return {k: row for k, (row, _) in rows.items()}, stopped

    # ---------- 2) 재무제표 ----------
    def enqueue(self, rows: dict, state: FsState) -> int:
        n = 0
        for rcept_no, row in sorted(rows.items()):
            rep = parse_periodic(row.get("report_nm", ""))
            if rep is None or not rep.changes_financials or row.get("corp_cls") not in FS_CORP_CLS or state.known(rcept_no):
                continue
            state.pending[rcept_no] = Pending(rcept_no, row["corp_code"].strip(), row.get("corp_name", "").strip(),
                                              row["report_nm"].strip(), row["rcept_dt"].strip(), (row.get("rm") or "").strip(),
                                              self.now().isoformat())
            n += 1
        return n

    def _candidates(self, item: Pending, state: FsState) -> list[tuple[int, str]]:
        rep = parse_periodic(item.report_nm)
        # 같은 회사·같은 기간의 보고서를 이미 받았다면 그때 맞았던 (연도, 코드)만 묻는다 (정정 보고서 대기 중 호출 절약)
        for d in state.done.values():
            if d["corp_code"] == item.corp_code and d["kind"] == rep.kind and d["period_end"] == rep.period_end.isoformat():
                return [(d["bsns_year"], d["reprt_code"])]
        return fs_candidates(rep)

    def _fetch(self, item: Pending, y: int, code: str, fs_div: str, run_id: str, m: BatchManifest):
        r = self.client.financial_statement(item.corp_code, y, code, fs_div)
        ref = self.raw.put(contract_id=OPENDART_FINANCIALS.contract_id, source_id=OPENDART_FINANCIALS.source_id,
                           response=r.response, request_id=run_id)
        returned = sorted({row.get("rcept_no", "") for row in r.rows})
        matched = r.status == OK and returned == [item.rcept_no]
        m.requests.append(_request_entry(r, ref.sha256, rcept_no=item.rcept_no, matched=matched, returned_rcept_no=returned))
        return r, ref.sha256, matched

    def collect_one(self, item: Pending, state: FsState, run_id: str, m: BatchManifest) -> bool:
        rep = parse_periodic(item.report_nm)
        found = None
        for y, code in self._candidates(item, state):
            r, sha, ok = self._fetch(item, y, code, "OFS", run_id, m)
            if ok:
                found = (y, code, [("OFS", r, sha)])
                break
        now = self.now()
        item.attempts += 1
        item.last_try_utc = now.isoformat()
        if found is None:
            if now - datetime.fromisoformat(item.first_seen_utc) >= timedelta(days=GIVE_UP_DAYS):
                state.gave_up[item.rcept_no] = {**asdict(item), "reason": f"{GIVE_UP_DAYS}일 동안 재무제표 API에 없음"}
                del state.pending[item.rcept_no]
                m.warnings.append(f"{item.rcept_no} {item.corp_name} {item.report_nm}: {GIVE_UP_DAYS}일 넘게 없어 대기열에서 뺌")
            m.count("not_yet")
            return False
        y, code, results = found
        # 연결재무제표는 항상 묻는다. 공시 목록 비고(rm)의 '연' 표시는 실제로 거의 쓰이지 않아(첫 수집 2,064건 중 1건)
        # 그것으로 연결 여부를 판단할 수 없다. 종속회사가 없는 회사는 013(데이터 없음)이 온다.
        r, sha, ok = self._fetch(item, y, code, "CFS", run_id, m)
        if ok:
            results.append(("CFS", r, sha))
        elif r.status != NO_DATA:
            m.warnings.append(f"{item.rcept_no}: 연결 재무제표 응답의 접수번호가 다름 {sorted({x.get('rcept_no') for x in r.rows})}")

        filing = FilingRef(item.rcept_no, item.corp_code, parse_rcept_dt(item.rcept_dt), rep.period_end)
        ingested = self.now()
        fs_divs, valid = [], []
        for fs_div, r, sha in results:
            schema = check_schema(r.rows, FS_REQUIRED, FS_KNOWN)
            if schema.missing:
                m.errors.append(f"{item.rcept_no} {fs_div}: 필수 필드 없음 {schema.missing}")
                continue
            if schema.unexpected:
                m.warnings.append(f"{item.rcept_no} {fs_div}: 새 필드 {schema.unexpected}")
            valid.append((fs_div, r, sha))
        # 정정 대상 찾기에 필요한 파일(그 회사)만 읽는다
        store = self.pit.load(parts=[(SERIES_FS, item.corp_code)])
        for fs_div, r, sha in valid:
            conv = fs_records(r.rows, filing=filing, fs_div=fs_div, contract_id=OPENDART_FINANCIALS.contract_id,
                              raw_sha=sha, ingested_at_utc=ingested, latest=store.latest)
            res = self.pit.append(conv.records, store=store)
            m.count("added", res.added)
            m.count("unchanged", res.unchanged + conv.unchanged_by_correction)
            m.count("older_than_stored", conv.older_than_stored)
            m.count("parse_errors", len(conv.parse_errors))
            m.warnings.extend(conv.parse_errors[:10])
            if res.conflicts:
                m.warnings.append(f"{item.rcept_no} {fs_div}: 같은 revision_id인데 내용이 다름 {list(res.conflicts)[:10]}")
            fs_divs.append(fs_div)
        state.done[item.rcept_no] = {"corp_code": item.corp_code, "corp_name": item.corp_name, "report_nm": item.report_nm,
                                     "rcept_dt": item.rcept_dt,
                                     "kind": rep.kind, "period_end": rep.period_end.isoformat(), "bsns_year": y,
                                     "reprt_code": code, "fs_divs": fs_divs, "cfs_checked": True,
                                     "done_at_utc": ingested.isoformat()}
        del state.pending[item.rcept_no]
        m.count("reports")
        return True

    # ---------- 3) XBRL: 사업부문·지역 ----------
    def collect_xbrl(self, fs_state: FsState, run_id: str, minutes: float) -> tuple[BatchManifest, Optional[str]]:
        """재무제표를 받은 정기보고서의 XBRL에서 부문·지역 숫자만 뽑아 저장한다. 원본 zip은 저장하지 않는다
        (서술형 주석이 들어 있어 계약상 저장·재배포하지 않음). 시간 예산 안에서 오래된 보고서부터 처리한다."""
        st = XbrlState.load(self.data_dir)
        todo = [k for k in sorted(fs_state.done) if k not in st.done and k not in st.gave_up]
        m = BatchManifest.start(run_id=run_id, contract=OPENDART_XBRL, code_version=self.version, now=self.now(),
                                target={"queued": len(todo), "minutes": minutes})
        deadline, stopped = self.clock() + minutes * 60, None
        for i, rcept_no in enumerate(todo):
            if self.clock() >= deadline:
                m.warnings.append(f"시간 예산 {minutes:g}분을 다 써서 {len(todo) - i}건은 다음 실행으로 넘김")
                break
            if self.client.budget.remaining < 2:
                m.warnings.append("호출 예산이 남지 않아 나머지는 다음 실행으로 넘김")
                break
            item = fs_state.done[rcept_no]
            t0 = self.clock()
            try:
                payload = fetch_xbrl(self.client, rcept_no, item["reprt_code"])
                doc = parse_xbrl_zip(payload)
            except BudgetExhausted as e:
                m.warnings.append(str(e))
                break
            except OpenDartError as e:
                if e.stop_all:
                    m.errors.append(f"{rcept_no}: {e}")
                    stopped = f"OpenDART status {e.status}"
                    break
                st.attempts[rcept_no] = st.attempts.get(rcept_no, 0) + 1
                if st.attempts[rcept_no] >= XBRL_MAX_ATTEMPTS:
                    st.gave_up[rcept_no] = {"reason": str(e), "attempts": st.attempts.pop(rcept_no)}
                    m.warnings.append(f"{rcept_no} {item.get('corp_name', '')}: XBRL을 {XBRL_MAX_ATTEMPTS}번 못 받아 포기 ({e})")
                    m.count("gave_up")
                else:
                    m.count("not_yet")
                continue
            except (ValueError, zipfile.BadZipFile, ET.ParseError) as e:
                st.gave_up[rcept_no] = {"reason": f"해석 실패: {type(e).__name__}: {e}"[:300]}
                m.warnings.append(f"{rcept_no}: XBRL 해석 실패 — 포기 ({type(e).__name__})")
                m.count("gave_up")
                continue
            except CollectorError as e:
                m.errors.append(f"{rcept_no}: {e}")
                continue
            ext = extract_segments(doc)
            ingested = self.now()
            store = self.pit.load(parts=[(SERIES_SEGMENTS, item["corp_code"])])
            recs, same = segment_records(ext, corp_code=item["corp_code"], rcept_no=rcept_no,
                                         rcept_dt=parse_rcept_dt(item.get("rcept_dt", rcept_no[:8])),
                                         payload_sha256=doc.payload_sha256, contract_id=OPENDART_XBRL.contract_id,
                                         ingested_at_utc=ingested, latest=store.latest)
            res = self.pit.append(recs, store=store)
            m.requests.append({"endpoint": "fnlttXbrl.xml", "rcept_no": rcept_no, "reprt_code": item["reprt_code"],
                               "sha256": doc.payload_sha256, "bytes": len(payload),
                               "seconds": round(self.clock() - t0, 1), "segment": ext.segment_status,
                               "geography": ext.geography_status, "values": len(ext.values)})
            m.count("reports")
            m.count("added", res.added)
            m.count("unchanged", res.unchanged + same)
            m.count(f"segment_{ext.segment_status.lower()}")
            if res.conflicts:
                m.warnings.append(f"{rcept_no}: 같은 revision_id인데 내용이 다름 {list(res.conflicts)[:10]}")
            st.attempts.pop(rcept_no, None)
            st.done[rcept_no] = {"corp_code": item["corp_code"], "corp_name": item.get("corp_name", ""),
                                 "segment": ext.segment_status, "geography": ext.geography_status,
                                 "values": len(ext.values), "done_at_utc": ingested.isoformat()}
        st.save(self.data_dir)
        m.target["remaining"] = len([k for k in fs_state.done if k not in st.done and k not in st.gave_up])
        m.finish(self.now())
        m.write(self.data_dir)
        return m, stopped

    # ---------- 전체 ----------
    def run(self, *, end: date, lookback_days: int, xbrl_minutes: float = XBRL_MINUTES) -> RunResult:
        started = self.now()
        run_id = f"{started:%Y%m%dT%H%M%SZ}"
        bgn = end - timedelta(days=lookback_days - 1)
        m_list = BatchManifest.start(run_id=run_id, contract=OPENDART_LIST, code_version=self.version, now=started,
                                     target={"bgn_de": bgn.isoformat(), "end_de": end.isoformat(), "corp_cls": list(LIST_CORP_CLS)})
        rows, stopped = self.collect_filings(bgn, end, run_id, m_list)
        m_list.finish(self.now())
        m_list.write(self.data_dir)
        if stopped and stopped != "budget":
            return RunResult(m_list, None, stopped)

        state = FsState.load(self.data_dir)
        added = self.enqueue(rows, state)
        m_fs = BatchManifest.start(run_id=run_id, contract=OPENDART_FINANCIALS, code_version=self.version, now=self.now(),
                                   target={"corp_cls": list(FS_CORP_CLS), "newly_queued": added,
                                           "pending_before": len(state.pending)})
        for rcept_no in sorted(state.pending):
            if self.client.budget.remaining < CALLS_PER_FS_ITEM:
                m_fs.warnings.append(f"호출 예산이 남지 않아 {len(state.pending)}건은 다음 실행으로 넘김")
                break
            try:
                self.collect_one(state.pending[rcept_no], state, run_id, m_fs)
            except BudgetExhausted as e:
                m_fs.warnings.append(str(e))
                break
            except OpenDartError as e:
                m_fs.errors.append(f"{rcept_no}: {e}")
                if e.stop_all:
                    stopped = f"OpenDART status {e.status}"
                    break
            except CollectorError as e:
                m_fs.errors.append(f"{rcept_no}: {e}")
        state.save(self.data_dir)
        m_fs.target["pending_after"] = len(state.pending)
        m_fs.finish(self.now())
        m_fs.write(self.data_dir)
        m_x = None
        if not (stopped and stopped != "budget") and xbrl_minutes > 0:
            m_x, x_stopped = self.collect_xbrl(state, run_id, xbrl_minutes)
            stopped = stopped or x_stopped
        return RunResult(m_list, m_fs, stopped if stopped != "budget" else None, m_x)


def check_connection(client: OpenDartClient, day: date) -> str:
    """기록 없이 인증키·연결만 확인: 그날 유가증권 공시 1건 조회."""
    r = client.call("list.json", {"bgn_de": f"{day:%Y%m%d}", "end_de": f"{day:%Y%m%d}", "corp_cls": "Y",
                                  "page_no": 1, "page_count": 1})
    return f"OpenDART 연결 정상 (status {r.status}, {day} 유가증권 공시 {r.data.get('total_count', 0)}건)"


def _summary(res: RunResult) -> str:
    lines = [f"공시 목록: {res.list_manifest.status} {res.list_manifest.counts}"]
    if res.fs_manifest:
        lines.append(f"재무제표: {res.fs_manifest.status} {res.fs_manifest.counts} 대기 {res.fs_manifest.target.get('pending_after')}")
    if res.xbrl_manifest:
        lines.append(f"사업부문(XBRL): {res.xbrl_manifest.status} {res.xbrl_manifest.counts} 남음 {res.xbrl_manifest.target.get('remaining')}")
    for m in filter(None, (res.list_manifest, res.fs_manifest, res.xbrl_manifest)):
        lines += [f"  경고: {w}" for w in m.warnings[:20]] + [f"  오류: {e}" for e in m.errors[:20]]
    if res.stopped:
        lines.append(f"중단: {res.stopped}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=os.environ.get("QF_DATA_DIR", "data"))
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--end-date", help="마지막 접수일 (YYYY-MM-DD, 기본: 어제 KST)")
    ap.add_argument("--max-calls", type=int, default=8000, help="이번 실행의 호출 상한 (일일 한도 20,000건을 다른 작업과 나눠 씀)")
    ap.add_argument("--check", action="store_true", help="기록 없이 인증키·연결만 확인")
    ap.add_argument("--xbrl-minutes", type=float, default=XBRL_MINUTES, help="사업부문 XBRL 단계 시간 예산(분), 0이면 건너뜀")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("DART_API_KEY 환경변수가 없음 (GitHub Actions에서는 레포 Secrets에 등록)", file=sys.stderr)
        return 2
    end = date.fromisoformat(args.end_date) if args.end_date else datetime.now(KST).date() - timedelta(days=1)
    client = OpenDartClient(key, budget=CallBudget(args.max_calls))
    try:
        if args.check:
            print(check_connection(client, end))
            return 0
        res = DailyRun(client, args.data_dir).run(end=end, lookback_days=args.lookback_days, xbrl_minutes=args.xbrl_minutes)
    except OpenDartError as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    print(_summary(res))
    print(f"호출 {client.budget.used}회 / 상한 {client.budget.max_calls}회")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
