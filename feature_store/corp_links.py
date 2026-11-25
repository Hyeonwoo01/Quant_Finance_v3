"""상장폐지 회사의 DART 고유번호 → 과거 종목코드 연결표 (재무 피처의 생존 편향을 줄이려고, 가이드 8-5).

    python -m feature_store.corp_links --data-dir private_data --public-dir public_data

OpenDART는 지금 상태의 종목코드만 주어서, 이미 상장폐지된 회사의 정기보고서에는 종목코드가 비어 있다.
그 회사의 재무를 시세와 이으려면 과거 종목코드가 필요하다. KRX 일별 시세의 종목명 이력과 DART 회사명을 맞춘다.

규칙 (정확도 우선, 애매하면 잇지 않는다):
  1) 이름: 공백·"(주)"·"㈜"·"주식회사"·마침표를 빼고 영문을 대문자로 바꾼 이름이 정확히 같아야 한다.
  2) 기간: 그 이름으로 거래된 기간이 그 회사 보고서의 [첫 기말 − 1년, 마지막 접수일]과 겹쳐야 한다.
  3) 하나뿐: 조건을 만족하는 종목코드가 둘 이상이면 잇지 않는다.
  4) 충돌 없음: 그 종목코드가 같은 기간에 종목코드가 있는 다른 DART 회사의 것이면 잇지 않는다.

KRX 종목명 이력에서 나온 표라 비공개 저장소(state/corp_code_links.json)에만 둔다.
연결은 "어느 회사의 재무가 어느 종목의 것인가"라는 식별 정보다. 재무 값과 그 가용 시각(시점 기준)은 바꾸지 않는다.
"""
import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

LINKS_FILE = Path("state") / "corp_code_links.json"
_DROP = re.compile(r"\(주\)|㈜|주식회사|\s|\.")


def normalize(name: str) -> str:
    return _DROP.sub("", name or "").upper()


def name_spans(panel_iter) -> pd.DataFrame:
    """(code, name)마다 그 이름으로 거래된 첫날·마지막 날. panel_iter: include_names 패널 조각들 (연도별 등)."""
    parts = []
    for p in panel_iter:
        g = p.groupby(["code", "name"])["date"].agg(["min", "max"]).reset_index()
        parts.append(g)
    s = pd.concat(parts, ignore_index=True).groupby(["code", "name"]).agg(first=("min", "min"), last=("max", "max"))
    s = s.reset_index()
    s["norm"] = s["name"].map(normalize)
    return s


def build_links(rows: list, spans: pd.DataFrame) -> dict:
    """rows: 정기보고서 행(ReportRow, 종목코드 있는 것·없는 것 모두). 반환: {"links": {corp: {...}}, "counts": {...}}."""
    unlisted, listed_use = defaultdict(list), defaultdict(list)
    for r in rows:
        if r.stock_code:
            listed_use[r.stock_code].append(r.period_end)
        else:
            unlisted[r.corp_code].append(r)
    by_norm = defaultdict(list)
    for t in spans.itertuples(index=False):
        by_norm[t.norm].append(t)
    links, counts = {}, {"corps_without_code": len(unlisted), "no_name_match": 0, "no_overlap": 0, "ambiguous": 0,
                         "conflict": 0, "linked": 0}
    for corp, rs in unlisted.items():
        name = rs[-1].corp_name
        cands = by_norm.get(normalize(name), [])
        if not cands:
            counts["no_name_match"] += 1
            continue
        lo = pd.Timestamp(min(r.period_end for r in rs)) - pd.Timedelta(days=365)
        hi = pd.Timestamp(max(r.rcept_dt for r in rs))
        ok = {}
        for t in cands:
            if t.last >= lo and t.first <= hi:
                ok.setdefault(t.code, t)
        if not ok:
            counts["no_overlap"] += 1
            continue
        if len(ok) > 1:
            counts["ambiguous"] += 1
            continue
        code, t = next(iter(ok.items()))
        mine = {r.period_end for r in rs}
        if any(pe in mine for pe in listed_use.get(code, [])):        # 같은 기간에 다른 회사가 그 종목코드를 씀
            counts["conflict"] += 1
            continue
        links[corp] = {"code": code, "dart_name": name, "krx_name": t.name, "traded_first": str(t.first.date()),
                       "traded_last": str(t.last.date()), "reports": len(rs)}
        counts["linked"] += 1
    return {"links": links, "counts": counts}


def load_links(data_dir) -> dict:
    p = Path(data_dir) / LINKS_FILE
    if not p.exists():
        return {}
    return {k: v["code"] for k, v in json.loads(p.read_text(encoding="utf-8"))["links"].items()}


def main(argv=None) -> int:
    from collectors.opendart_history import SERIES_REPORT_INDEX, rows_from_records
    from core.source_registry import default_registry
    from feature_store.price_panel import load_price_panel
    from storage.pit_store import PITFileStore
    from storage.visibility import is_private_store
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True, help="비공개 저장소 (KRX 시세, 연결표를 쓸 곳)")
    ap.add_argument("--public-dir", required=True, help="공개 data 브랜치 (정기보고서 목록)")
    args = ap.parse_args(argv)
    if not is_private_store(Path(args.data_dir)):
        raise PermissionError("연결표는 KRX 종목명 이력에서 나와 비공개 저장소에만 쓴다")
    pit = PITFileStore(args.public_dir, default_registry(), parquet_series={SERIES_REPORT_INDEX})
    rows = rows_from_records(pit.iter_records([SERIES_REPORT_INDEX]))
    years = range(2010, datetime.now(timezone.utc).year + 1)
    spans = name_spans(load_price_panel(args.data_dir, date(y, 1, 1), date(y, 12, 31), include_names=True)[["code", "name", "date"]]
                       for y in years)
    res = build_links(rows, spans)
    out = {"built_at_utc": datetime.now(timezone.utc).isoformat(), "rule": "정규화한 이름 일치 + 거래 기간 겹침 + 후보 하나 + 충돌 없음",
           **res}
    p = Path(args.data_dir) / LINKS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"종목코드 없는 회사 {res['counts']['corps_without_code']:,}개 → 연결 {res['counts']['linked']:,}개 "
          f"(이름 불일치 {res['counts']['no_name_match']:,}, 기간 안 겹침 {res['counts']['no_overlap']:,}, "
          f"후보 여럿 {res['counts']['ambiguous']:,}, 충돌 {res['counts']['conflict']:,}) — 건수만 표시")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
