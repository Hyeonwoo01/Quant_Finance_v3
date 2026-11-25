"""재무 피처 커버리지를 생존 종목과 상장폐지 종목으로 나눠 본다 (생존 편향 점검, 모델은 돌리지 않음).

    python -m evaluation.coverage_report --data-dir private_data --public-dir public_data

과거 재무 채우기는 종목코드가 있는 정기보고서만 받는다. 그런데 상장폐지된 회사는 지금 종목코드가 지워져 있어
재무가 빠진다. 학습·검증 표본에서 나중에 상장폐지된 종목의 재무 커버리지가 생존 종목보다 크게 낮으면,
재무 피처가 "살아남을 종목"이라는 정보를 몰래 담게 된다 (결측 자체가 미래 정보).

출력은 비율·건수뿐이다 (시세 값 없음). Vault 구간 시세는 읽지 않는다. 실험 기록에 남기지 않는다 (시행이 아님).
"""
import argparse
from datetime import date, timedelta

import pandas as pd

from evaluation.run_experiment import build_dataset
from feature_store import fundamental_features as fund
from feature_store.price_panel import load_price_panel

DELIST_GAP_DAYS = 30        # 패널 마지막 날보다 이만큼 먼저 행이 끊긴 종목을 "그 사이 상장폐지"로 본다


def coverage_by_survival(df: pd.DataFrame, last_date: pd.Series, panel_end: pd.Timestamp) -> dict:
    """df: build_dataset 결과(재무 피처 포함). last_date: 종목별 패널 마지막 날짜."""
    gone = last_date < panel_end - pd.Timedelta(days=DELIST_GAP_DAYS)
    d = df.assign(delisted=df["code"].map(gone).fillna(False).astype(bool), has_fin=df["fin_age"].notna(),
                  year=df["date"].dt.year)
    out = {"rows": int(len(d)), "delisted_rows": int(d["delisted"].sum()),
           "delisted_codes": int(d.loc[d["delisted"], "code"].nunique()), "codes": int(d["code"].nunique()), "by_year": {}}
    for y, g in d[d["year"] >= 2017].groupby("year"):
        s, x = g[~g["delisted"]], g[g["delisted"]]
        out["by_year"][int(y)] = {"survivor": round(float(s["has_fin"].mean()), 3) if len(s) else None,
                                  "delisted": round(float(x["has_fin"].mean()), 3) if len(x) else None,
                                  "delisted_share": round(float(len(x) / len(g)), 3)}
    late = d[d["year"] >= 2017]
    out["survivor_2017_on"] = round(float(late.loc[~late["delisted"], "has_fin"].mean()), 3)
    out["delisted_2017_on"] = round(float(late.loc[late["delisted"], "has_fin"].mean()), 3) if late["delisted"].any() else None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--public-dir", required=True)
    ap.add_argument("--target", default="RET_21D")
    ap.add_argument("--every", type=int, default=21)
    ap.add_argument("--liquidity-pct", type=float, default=0.2)
    ap.add_argument("--vault-start", default="2024-11-01")
    args = ap.parse_args(argv)
    vault = date.fromisoformat(args.vault_start)
    panel = load_price_panel(args.data_dir, date(2010, 1, 1), vault - timedelta(days=1))
    from feature_store.corp_links import load_links
    links = load_links(args.data_dir)
    print(f"상장폐지 회사 연결표: {len(links):,}개 회사" if links else "상장폐지 회사 연결표 없음")
    reports = fund.report_table(fund.load_fin_hist(args.public_dir, links))
    df = build_dataset(panel, args.target, args.every, args.liquidity_pct, 252, reports=reports)
    last = panel.groupby("code")["date"].max()
    r = coverage_by_survival(df, last, panel["date"].max())
    print(f"표본 {r['rows']:,}행 · 종목 {r['codes']:,}개 중 그 뒤 상장폐지(패널 끝 {DELIST_GAP_DAYS}일 전에 행이 끊김) "
          f"{r['delisted_codes']:,}개 · {r['delisted_rows']:,}행")
    print(f"재무가 붙은 비율 (2017~): 생존 {r['survivor_2017_on']} vs 상장폐지 {r['delisted_2017_on']}")
    print("연도별 (생존 / 상장폐지 / 상장폐지 종목 비중):")
    for y, v in r["by_year"].items():
        print(f"  {y}: {v['survivor']} / {v['delisted']} / {v['delisted_share']}")
    print("자료: 한국거래소 통계정보 (KRX Open API)·금융감독원 전자공시 — 비율만 표시")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
