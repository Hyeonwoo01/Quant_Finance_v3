"""상장폐지 회사 연결표: 이름 정규화, 기간 겹침, 후보 여럿·충돌이면 잇지 않음, 재무 로딩에 반영."""
from datetime import date

import pandas as pd

from collectors.opendart_history import ReportRow
from feature_store.corp_links import build_links, name_spans, normalize


def rr(corp, stock, name, pe, rd):
    return ReportRow(corp, stock, name, "사업보고서", pe, (), f"{rd:%Y%m%d}000001", rd)


def spans():
    p = pd.DataFrame([
        ("100001", "에이비씨", "2015-01-02"), ("100001", "에이비씨", "2019-06-28"),          # 2019년 상장폐지
        ("100002", "디이에프", "2012-01-02"), ("100002", "디이에프", "2013-12-30"),          # 너무 일찍 폐지
        ("100003", "동명", "2016-01-04"), ("100003", "동명", "2020-12-30"),
        ("100004", "동명", "2016-01-04"), ("100004", "동명", "2020-12-30"),                  # 같은 이름 둘 → 애매
        ("100005", "지에이치", "2016-01-04"), ("100005", "지에이치", "2020-12-30")],
        columns=["code", "name", "date"])
    p["date"] = pd.to_datetime(p["date"])
    return name_spans([p])


def test_normalize():
    assert normalize("(주)에이 비.씨") == normalize("에이비씨㈜") == normalize("주식회사 에이비씨") == "에이비씨"
    assert normalize("abc홀딩스") == "ABC홀딩스"


def test_build_links_rules():
    rows = [rr("C1", "", "(주)에이비씨", date(2017, 12, 31), date(2018, 3, 30)),      # → 100001
            rr("C2", "", "디이에프", date(2017, 12, 31), date(2018, 3, 30)),          # 기간 안 겹침
            rr("C3", "", "동명", date(2017, 12, 31), date(2018, 3, 30)),              # 후보 둘
            rr("C4", "", "없는회사", date(2017, 12, 31), date(2018, 3, 30)),          # 이름 없음
            rr("C5", "", "지에이치", date(2017, 12, 31), date(2018, 3, 30)),          # 다른 회사가 그 코드를 씀
            rr("C9", "100005", "지에이치홀딩스", date(2017, 12, 31), date(2018, 3, 30))]
    res = build_links(rows, spans())
    assert res["links"] == {"C1": {"code": "100001", "dart_name": "(주)에이비씨", "krx_name": "에이비씨",
                                   "traded_first": "2015-01-02", "traded_last": "2019-06-28", "reports": 1}}
    assert res["counts"] == {"corps_without_code": 5, "no_name_match": 1, "no_overlap": 1, "ambiguous": 1,
                             "conflict": 1, "linked": 1}


def test_load_fin_hist_uses_links_only_for_blank_codes(tmp_path):
    from datetime import datetime, timezone
    from feature_store.fundamental_features import load_fin_hist
    from orchestration.dart_history import HistoryRun
    from tests.unit.test_dart_history import NOW, FakeDart
    from tests.unit.test_opendart import client
    HistoryRun(client(FakeDart(), max_calls=200), tmp_path, now=lambda: NOW, version="t", log=lambda s: None).run(
        date(2017, 1, 1), date(2017, 6, 30))
    f0 = load_fin_hist(tmp_path)
    f1 = load_fin_hist(tmp_path, {"00000001": "999999"})              # 종목코드가 이미 있으면 바꾸지 않음
    assert list(f0["stock_code"]) == list(f1["stock_code"]) == ["000001"] and not f1["linked"].any()
