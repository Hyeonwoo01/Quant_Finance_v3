"""OpenDART 과거 재무 채우기: 정기보고서 목록 → 마지막 재무 변경일 기준 가용 시각, 연결 우선, 비표준·비상장 제외."""
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

from collectors.opendart_history import SERIES_FIN_HIST, SERIES_REPORT_INDEX, last_change, report_rows
from core.availability import backtest_available_at
from core.calendar_align import dart_available_at
from orchestration.dart_history import HistoryRun
from storage.pit_store import read_parquet
from tests.unit.test_opendart import client

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)


def lrow(corp, stock, name, rcept_no, rcept_dt):
    return {"corp_code": corp, "corp_name": "회사" + corp[-1], "stock_code": stock, "corp_cls": "Y", "report_nm": name,
            "rcept_no": rcept_no, "flr_nm": "회사", "rcept_dt": rcept_dt, "rm": ""}


LIST = [lrow("00000001", "000001", "사업보고서 (2016.12)", "20170331000001", "20170331"),
        lrow("00000001", "000001", "[기재정정]사업보고서 (2016.12)", "20170510000002", "20170510"),
        lrow("00000001", "000001", "[첨부정정]사업보고서 (2016.12)", "20170601000003", "20170601"),   # 숫자 안 바뀜
        lrow("00000002", "000002", "사업보고서 (2016.03)", "20160630000004", "20160630"),            # 3월 결산
        lrow("00000003", "", "사업보고서 (2016.12)", "20170331000005", "20170331"),                  # 비상장
        lrow("00000004", "000004", "주요사항보고서(유상증자결정)", "20170401000006", "20170401")]


def acct(corp, fs, name, amt, sj="IS", cum=None, prev_cum=None, rcept="20170331000001"):
    r = {"rcept_no": rcept, "reprt_code": "11011", "bsns_year": "2016", "corp_code": corp, "stock_code": "000001",
         "fs_div": fs, "sj_div": sj, "account_nm": name, "thstrm_amount": f"{amt:,}", "frmtrm_amount": "1,000"}
    if cum is not None:
        r["thstrm_add_amount"], r["frmtrm_add_amount"] = f"{cum:,}", f"{prev_cum:,}"
    return r


class FakeDart:
    def __init__(self):
        self.calls = []

    def __call__(self, url, params, timeout, headers=None):
        ep = urlparse(url).path.rsplit("/", 1)[-1]
        self.calls.append((ep, dict(params)))
        if ep == "list.json":
            rows = [r for r in LIST if params["bgn_de"] <= r["rcept_dt"] <= params["end_de"]] if params.get("pblntf_ty") == "A" else []
            body = {"status": "000" if rows else "013", "message": "", "page_no": 1, "total_page": 1,
                    "total_count": len(rows), "list": rows}
        elif ep == "fnlttSinglAcnt.json":                       # 단일회사 주요계정: 같은 필드
            rows = []
            if params["corp_code"] == "00000001" and params["bsns_year"] == "2016" and params["reprt_code"] == "11011":
                rows = [acct("00000001", "CFS", "매출액", 500), acct("00000001", "CFS", "자본총계", 2000, sj="BS")]
            body = {"status": "000" if rows else "013", "message": "", "list": rows}
        elif ep == "fnlttMultiAcnt.json":
            rows = []
            if "00000001" in params["corp_code"] and params["bsns_year"] == "2016" and params["reprt_code"] == "11011":
                rows = [acct("00000001", "CFS", "매출액", 500), acct("00000001", "OFS", "매출액", 300),
                        acct("00000001", "CFS", "자본총계", 2000, sj="BS"), acct("00000001", "CFS", "당기순이익(손실)", 50)]
            body = {"status": "000" if rows else "013", "message": "", "list": rows}
        else:
            body = {"status": "013", "message": ""}
        return 200, {}, json.dumps(body, ensure_ascii=False).encode()


def test_report_rows_and_last_change():
    rows, counts = report_rows(LIST)
    assert counts == {"not_periodic": 1, "non_standard": 1, "no_stock_code": 1}
    assert last_change(rows) == {("00000001", 2016, "11011"): date(2017, 5, 10)}      # 첨부정정은 숫자 변경 아님


def test_history_run_uses_last_correction_date(tmp_path):
    fake = FakeDart()
    run = HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None)
    m = run.run(date(2017, 1, 1), date(2017, 6, 30))
    assert m.status == "ok" and m.counts["fin_added"] == 1 and m.counts["index_added"] == 4    # 종목코드 없는 회사도 목록에
    assert all(p.get("pblntf_ty") == "A" and "corp_cls" not in p for e, p in fake.calls if e == "list.json")
    rec = read_parquet(tmp_path / "pit_versions" / SERIES_FIN_HIST / "2016-01.parquet")[0]
    v = json.loads(rec.value)
    assert v["fs_div"] == "CFS" and v["accounts"]["revenue"]["amt"] == 500 and v["accounts"]["equity"]["amt"] == 2000
    # 응답 접수번호는 원래 제출(3/31)이지만, 5/10 기재정정 뒤의 값일 수 있으므로 5/10 기준
    assert rec.provider_available_at_utc == dart_available_at(date(2017, 5, 10)).to_pydatetime()
    assert backtest_available_at(rec) == rec.provider_available_at_utc
    assert rec.available_at_utc > datetime(2026, 11, 1, tzinfo=UTC)                 # 실제로 받은 시각 기준은 지금
    idx = read_parquet(tmp_path / "pit_versions" / SERIES_REPORT_INDEX / "2016-12.parquet")
    assert {json.loads(r.value)["rcept_dt"] for r in idx} == {"2017-03-31", "2017-05-10", "2017-06-01"}
    m2 = HistoryRun(client(FakeDart(), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None).run(
        date(2017, 1, 1), date(2017, 6, 30))
    assert m2.counts.get("fin_added", 0) == 0 and m2.counts["fin_unchanged"] == 1   # 다시 돌려도 그대로


class FlakyDart(FakeDart):
    """처음 n번의 공시 목록 요청에 점검 페이지(HTML)를 돌려준다."""

    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    def __call__(self, url, params, timeout, headers=None):
        if urlparse(url).path.endswith("list.json") and self.fail_times > 0:
            self.fail_times -= 1
            self.calls.append(("list.json", dict(params)))
            return 200, {}, b"<html>maintenance</html>"
        return super().__call__(url, params, timeout, headers)


def test_transient_bad_response_is_retried(tmp_path):
    run = HistoryRun(client(FlakyDart(1), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None,
                     sleep=lambda s: None)
    m = run.run(date(2017, 1, 1), date(2017, 6, 30))
    assert m.status == "ok" and m.counts["retries"] == 1 and m.counts["fin_added"] == 1


def test_missing_list_window_blocks_value_fetch(tmp_path):
    fake = FlakyDart(3)                                  # 첫 기간이 세 번 다 실패 → 그 기간을 건너뜀
    run = HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None,
                     sleep=lambda s: None)
    m = run.run(date(2017, 1, 1), date(2017, 6, 30))
    assert m.status == "failed" and m.counts["skipped_units"] == 1 and "값 받기를 하지 않음" in m.errors[0]
    assert not any(e == "fnlttMultiAcnt.json" for e, _ in fake.calls)            # 정정 이력이 불완전하면 값을 받지 않음


class OldYearDart(FakeDart):
    """2015년 이후에 늦게 낸 2013 사업연도 보고서가 섞인 목록."""

    def __call__(self, url, params, timeout, headers=None):
        if urlparse(url).path.endswith("list.json") and params["bgn_de"] <= "20170105" <= params["end_de"]:
            rows = [lrow("00000009", "000009", "사업보고서 (2013.12)", "20170105000009", "20170105")]
            body = {"status": "000", "message": "", "page_no": 1, "total_page": 1, "total_count": 1, "list": rows}
            self.calls.append(("list.json", dict(params)))
            return 200, {}, json.dumps(body, ensure_ascii=False).encode()
        return super().__call__(url, params, timeout, headers)


def test_pre_api_years_are_indexed_but_not_fetched(tmp_path):
    fake = OldYearDart()
    m = HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None).run(
        date(2017, 1, 1), date(2017, 6, 30))
    assert m.status == "ok" and m.counts["value_skipped_pre_api_year"] == 1 and m.counts["fin_added"] == 1
    assert all(p["bsns_year"] >= "2015" for e, p in fake.calls if e == "fnlttMultiAcnt.json")


def test_from_index_reuses_a_complete_scan_only(tmp_path):
    mk = lambda fake: HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None)
    m0 = mk(FakeDart()).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True)
    assert m0.status == "failed" and "from_index 없이" in m0.errors[0]              # 끝난 훑기 기록이 없으면 거절
    mk(FakeDart()).run(date(2017, 1, 1), date(2017, 6, 30))
    assert json.loads((tmp_path / "state" / "dart_history_scan.json").read_text())["rows"] == 4
    from collectors.opendart_history import rows_from_records
    stored = rows_from_records(mk(FakeDart()).pit.iter_records([SERIES_REPORT_INDEX]))
    assert stored == sorted(report_rows(LIST, keep_unlisted=True)[0], key=lambda r: (r.rcept_dt, r.rcept_no))   # 다시 만든 행이 원래와 같다
    fake = FakeDart()
    m = mk(fake).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True)
    assert not any(e == "list.json" for e, _ in fake.calls) and m.counts["index_rows_reused"] == 4
    assert m.status == "ok" and m.counts["fin_unchanged"] == 1
    assert mk(FakeDart()).run(date(2016, 1, 1), date(2017, 6, 30), from_index=True).status == "failed"   # 시작이 범위 밖
    m4 = mk(FakeDart()).run(date(2017, 1, 1), date(2017, 7, 2), from_index=True)                       # 끝은 줄여서 씀
    assert m4.status == "ok" and m4.counts["index_rows_reused"] == 4 and "2017-06-30까지" in m4.warnings[0]


def test_incomplete_scan_leaves_no_state(tmp_path):
    HistoryRun(client(FlakyDart(3), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None,
               sleep=lambda s: None).run(date(2017, 1, 1), date(2017, 6, 30))
    assert not (tmp_path / "state" / "dart_history_scan.json").exists()


class FlakyValues(FakeDart):
    """처음 n번의 주요계정 요청에 점검 페이지를 돌려준다."""

    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    def __call__(self, url, params, timeout, headers=None):
        if urlparse(url).path.endswith("fnlttMultiAcnt.json") and self.fail_times > 0:
            self.fail_times -= 1
            self.calls.append(("fnlttMultiAcnt.json", dict(params)))
            return 200, {}, b"<html>maintenance</html>"
        return super().__call__(url, params, timeout, headers)


def test_only_missing_fills_skipped_batches(tmp_path):
    mk = lambda fake: HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None,
                                 sleep=lambda s: None)
    m = mk(FlakyValues(3)).run(date(2017, 1, 1), date(2017, 6, 30))
    assert m.counts["skipped_units"] == 1 and m.counts.get("fin_added", 0) == 0     # 값 묶음만 건너뜀, 목록은 완전
    m2 = mk(FakeDart()).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True, only_missing=True)
    assert m2.status == "ok" and m2.counts["fin_added"] == 1
    fake = FakeDart()
    m3 = mk(fake).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True, only_missing=True)
    assert m3.counts["value_already_stored"] == 1                                    # 저장된 회사는 다시 묻지 않음
    assert not any(e == "fnlttMultiAcnt.json" and "00000001" in p["corp_code"] for e, p in fake.calls)


def test_reindex_from_raw_adds_unlisted_rows_without_api_calls(tmp_path):
    """예전 방식(종목코드 없는 회사를 버림)으로 쌓인 목록을 저장된 원본에서 다시 만들어 채운다."""
    import gzip
    from collectors.opendart_history import index_records
    from core.source_registry import OPENDART_LIST
    run = HistoryRun(client(FakeDart(), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None)
    old, _ = report_rows(LIST)                                           # 종목코드 있는 행만 (예전 목록)
    run.pit.append(index_records(old, contract_id=OPENDART_LIST.contract_id, raw_sha="x", ingested_at_utc=NOW,
                                 latest=lambda k: None))
    body = json.dumps({"status": "000", "list": LIST}, ensure_ascii=False).encode()
    d = tmp_path / "raw" / OPENDART_LIST.source_id / "objects" / "ab"
    d.mkdir(parents=True)
    (d / "ab12.gz").write_bytes(gzip.compress(body))
    fake = FakeDart()
    run2 = HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None)
    from orchestration.dart_history import BatchManifest, OPENDART_FINANCIALS
    m = BatchManifest.start(run_id="r", contract=OPENDART_FINANCIALS, code_version="t", now=NOW, target={})
    assert run2.reindex_from_raw(m) == 1 and fake.calls == []            # 00000003(비상장) 한 건만 새로, 호출 없음
    assert run2.reindex_from_raw(m) == 0                                 # 다시 해도 그대로


class NoMultiDart(FakeDart):
    """다중회사 주요계정은 늘 빈 응답 (그 회사를 빠뜨리는 경우)."""

    def __call__(self, url, params, timeout, headers=None):
        if urlparse(url).path.endswith("fnlttMultiAcnt.json"):
            self.calls.append(("fnlttMultiAcnt.json", dict(params)))
            return 200, {}, json.dumps({"status": "013", "message": ""}).encode()
        return super().__call__(url, params, timeout, headers)


def test_single_account_fills_what_multi_missed(tmp_path):
    mk = lambda fake: HistoryRun(client(fake, max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None)
    m = mk(NoMultiDart()).run(date(2017, 1, 1), date(2017, 6, 30))
    assert m.counts.get("fin_added", 0) == 0
    fake = FakeDart()
    m2 = mk(fake).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True, single=True)
    singles = [p for e, p in fake.calls if e == "fnlttSinglAcnt.json"]
    assert m2.status == "ok" and m2.counts["single_fin_added"] == 1 and len(singles) == 1   # 종목코드 있는 회사만
    rec = read_parquet(tmp_path / "pit_versions" / SERIES_FIN_HIST / "2016-01.parquet")[0]
    assert rec.provider_available_at_utc == dart_available_at(date(2017, 5, 10)).to_pydatetime()  # 같은 가용 시각 규칙
    fake3 = FakeDart()
    mk(fake3).run(date(2017, 1, 1), date(2017, 6, 30), from_index=True, single=True)
    assert not any(e == "fnlttSinglAcnt.json" for e, _ in fake3.calls)                       # 이미 있으면 묻지 않음
