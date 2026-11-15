"""주가 공급원 대조: 같은 기준일·같은 종목의 값이 공급원끼리 일치하는지 (저장 없음, 비공개 저장소를 읽기만 한다).

    python -m orchestration.compare_prices --data-dir private_data [--a fsc --b krx] [--last 20]

공급원을 고를 때 "어느 쪽이 더 정확한가"를 말로 정하지 않고 겹치는 날의 값으로 잰다. 한국거래소가 원천이므로 공급원끼리
값이 다르면 어느 쪽이 KRX 정규장 기준이 아닌지(예: 넥스트레이드 거래 포함 여부) 따로 확인해야 한다.
공개 로그에 남으므로 시세 값은 출력하지 않는다. 일치율·건수·필드 이름만 출력한다.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from collectors.fsc_krx import SERIES_PRICE
from collectors.krx_openapi import SERIES_KRX_PRICE
from core.source_registry import default_registry
from storage.pit_store import PITFileStore

SERIES = {"fsc": SERIES_PRICE, "krx": SERIES_KRX_PRICE}
FIELDS = ("open", "high", "low", "close", "volume", "value", "shares", "mktcap")


def short_code(code: str) -> str:
    """종목 단축코드(6자리). ISIN(KR7005930003)이면 4~9번째 글자, 'A005930' 같은 접두사는 뗀다."""
    c = (code or "").strip()
    if len(c) == 12 and c[:2].isalpha():
        return c[3:9]
    return c[-6:] if len(c) > 6 else c


def _store(data_dir: Path) -> PITFileStore:
    return PITFileStore(data_dir, default_registry(), parquet_series=set(SERIES.values()))


def load_day(data_dir: Path, series: str, day: str) -> dict:
    """그날 파일의 종목별 최신 값 {단축코드: {field: value}} (같은 관측값의 여러 버전 중 마지막에 받은 것)."""
    out, when = {}, {}
    for r in _store(data_dir).iter_records(parts=[(series, day)]):
        attrs = json.loads(r.attributes or "{}")
        code = short_code(attrs.get("srtnCd") or attrs.get("code") or r.entity_id.split(":")[-1])
        if code not in when or r.ingested_at_utc >= when[code]:
            out[code], when[code] = json.loads(r.value), r.ingested_at_utc
    return out


def _days(data_dir: Path, series: str) -> set:
    d = data_dir / "pit_versions" / series
    return {p.stem for p in d.glob("*.jsonl")} | {p.stem for p in d.glob("*.parquet")}


def compare(data_dir, a: str = "fsc", b: str = "krx", last: int = 20) -> dict:
    data_dir = Path(data_dir)
    days_a, days_b = _days(data_dir, SERIES[a]), _days(data_dir, SERIES[b])
    days = sorted(days_a & days_b)[-last:] if last else sorted(days_a & days_b)
    match, total, only_a, only_b, both = Counter(), Counter(), 0, 0, 0
    worst = Counter()
    for d in days:
        va, vb = load_day(data_dir, SERIES[a], d), load_day(data_dir, SERIES[b], d)
        only_a += len(va.keys() - vb.keys())
        only_b += len(vb.keys() - va.keys())
        for code in va.keys() & vb.keys():
            both += 1
            for f in FIELDS:
                x, y = va[code].get(f), vb[code].get(f)
                if x is None or y is None:
                    continue
                total[f] += 1
                if x == y:
                    match[f] += 1
                else:
                    worst[f] += 1
    rates = {f: (match[f] / total[f]) if total[f] else None for f in FIELDS}
    return {"a": a, "b": b, "days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None,
            "pairs": both, f"only_{a}": only_a, f"only_{b}": only_b, "match_rate": rates,
            "mismatches": dict(worst)}


def summary(res: dict) -> str:
    if not res["days"]:
        return f"{res['a']}·{res['b']} 둘 다 있는 기준일이 없음"
    lines = [f"공급원 대조 {res['a']} vs {res['b']}: 기준일 {res['days']}일 ({res['first']} ~ {res['last']}), "
             f"같은 종목 {res['pairs']:,}쌍, {res['a']}에만 {res[f'only_' + res['a']]:,} · {res['b']}에만 {res[f'only_' + res['b']]:,}"]
    for f in FIELDS:
        r = res["match_rate"][f]
        lines.append(f"  {f:7s} 일치율 {'—' if r is None else f'{r:.4%}'} (불일치 {res['mismatches'].get(f, 0):,}건)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="private_data")
    ap.add_argument("--a", choices=sorted(SERIES), default="fsc")
    ap.add_argument("--b", choices=sorted(SERIES), default="krx")
    ap.add_argument("--last", type=int, default=20, help="겹치는 기준일 중 최근 며칠 (0이면 전부)")
    args = ap.parse_args(argv)
    print(summary(compare(args.data_dir, args.a, args.b, args.last)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
