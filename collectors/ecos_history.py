"""ECOS 금리 과거 채우기 — 발표 시각 규칙을 붙인 시리즈 bok_ecos_hist (가이드 5-4·5-5, 4-4).

매일 수집(bok_ecos)은 받은 시각을 가용 시각으로 둬 최근 24~45개월만 있다. 백테스트에 쓰려면 과거 값에
"그 값을 언제부터 알 수 있었나"를 붙여야 한다. 공급자가 발표 시각을 주지 않으므로 보수적인 규칙으로 정한다:

  month_end  월간 값(기준금리 월말): 그 달 마지막 날 다음 KRX 세션의 08:30 KST
  daily_t2   일간 값(국고채 3년 최종호가수익률): 그날 다음다음 KRX 세션의 08:30 KST
             (금융투자협회는 당일 오후에 공표하고 ECOS 반영은 대개 다음 영업일이라, 시세의 D−2 규칙과 같게 둔다)

대상은 정정·재기준이 없는 금리만이다. 지수(수출물가 등)는 기준연도 개편 때 과거 값이 다시 계산되므로
지금 받은 과거 값이 그때 알 수 있던 값과 다르다 → 과거 채우기에 넣지 않는다 (vintage 위험).
가용 시각은 provider_available_at_utc에 두고, available_at_utc는 max(공급자 시각, 받은 뒤 첫 컷오프)다.
"""
import hashlib
import json
from datetime import date, datetime
from typing import Callable, Optional

import pandas as pd

from collectors.macro_sources import Observation
from core.calendar_align import first_kr_cutoff_at_or_after, kr_decision_cutoff, next_session_after
from core.pit_schema import PITRecord

SERIES_ECOS_HIST = "bok_ecos_hist"
RULES = ("month_end", "daily_t2")
START = {"M": "200901", "D": "20090101"}


def provider_time(rule: str, period_end: date) -> datetime:
    if rule == "month_end":
        s = next_session_after(pd.Timestamp(period_end))
    elif rule == "daily_t2":
        s = next_session_after(next_session_after(pd.Timestamp(period_end)))
    else:
        raise ValueError(f"알 수 없는 발표 시각 규칙: {rule}")
    return kr_decision_cutoff(s).to_pydatetime()


def history_records(obs: list, *, rule: str, contract_id: str, raw_sha: str, ingested_at_utc: datetime,
                    source_url: str, latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, int]:
    """관측 → 공급자 시각을 붙인 PIT 레코드. 이미 같은 값이 있으면 넣지 않는다 (반환: 레코드, 건너뛴 수)."""
    floor = first_kr_cutoff_at_or_after(ingested_at_utc).to_pydatetime()
    out, same, seen = [], 0, set()
    for o in obs:
        if o.value is None:
            continue
        metric = f"{o.series_key}|{o.metric}"
        key = (metric, o.period_start, o.period_end)
        if key in seen:
            continue
        seen.add(key)
        prov = provider_time(rule, o.period_end)
        vhash = hashlib.sha256(repr(o.value).encode()).hexdigest()[:10]
        rec = PITRecord(entity_id="KR", series_id=SERIES_ECOS_HIST, metric_id=metric,
                        available_at_utc=max(prov, floor), ingested_at_utc=ingested_at_utc,
                        revision_id=f"{metric}|{o.period_end}|{vhash}", value=o.value, unit=o.unit,
                        source_provider="BOK", raw_payload_hash=raw_sha, license_contract_id=contract_id,
                        observation_period_start=o.period_start, observation_period_end=o.period_end,
                        provider_available_at_utc=prov, source_url=source_url,
                        attributes=json.dumps({"label": o.label, "availability_rule": rule}, ensure_ascii=False))
        prev = latest(rec.observation_key)
        if prev is not None:
            if prev.value == rec.value:
                same += 1
                continue
            raise ValueError(f"{metric} {o.period_end}: 과거 값이 바뀜 ({prev.value} → {rec.value}) — 금리는 정정이 없다고 "
                             f"보고 과거 채우기를 했으므로 멈추고 확인한다")
        out.append(rec)
    return out, same


def as_observations(rows) -> list:
    """테스트·재사용용: (series_key, period_end, value, cycle) → Observation."""
    out = []
    for key, end, value, cycle in rows:
        start = end if cycle == "D" else date(end.year, end.month, 1)
        out.append(Observation(key, "value", start, end, value, "%", ""))
    return out
