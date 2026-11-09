"""무작위 사례로 PIT 불변조건을 확인한다: 쓰인 값은 모두 의사결정(as_of) 시각 이전에 이용 가능했어야 한다.

외부 라이브러리 없이 고정 시드로 반복한다 (실패하면 시드로 그대로 재현).
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from core.calendar_align import align_asof
from core.data_contracts import Purpose
from core.pit_schema import PITRecord, PITStore

SEEDS = range(30)
T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _random_times(rng, n, minutes=60 * 24 * 30):
    # 분 단위 정수로 뽑아 같은 시각(경계)이 자주 나오게 한다
    return T0 + pd.to_timedelta(rng.integers(0, minutes, n), unit="min")


@pytest.mark.parametrize("seed", SEEDS)
def test_align_asof_matches_brute_force(seed):
    rng = np.random.default_rng(seed)
    series = [f"S{i}" for i in range(rng.integers(1, 4))]
    entities = [f"E{i}" for i in range(rng.integers(1, 4))]          # 여러 회사가 같은 series_id를 쓴다
    grid = pd.DataFrame({"decision_time_utc": _random_times(rng, 40), "entity_id": rng.choice(entities, 40),
                         "series_id": rng.choice(series, 40)})
    obs = pd.DataFrame({"available_at_utc": _random_times(rng, 60), "entity_id": rng.choice(entities, 60),
                        "series_id": rng.choice(series, 60),
                        "value": rng.normal(size=60)}).drop_duplicates(["entity_id", "series_id", "available_at_utc"])

    out = align_asof(grid, obs)
    assert len(out) == len(grid)
    for i, row in out.iterrows():
        cand = obs[(obs.entity_id == row.entity_id) & (obs.series_id == row.series_id)
                   & (obs.available_at_utc <= row.decision_time_utc)]
        if cand.empty:
            assert row.match_status == "NO_DATA" and pd.isna(row.value)
        else:
            best = cand.loc[cand.available_at_utc.idxmax()]
            assert row.match_status == "OK"
            assert row.available_at_utc == best.available_at_utc <= row.decision_time_utc
            assert row.value == best.value


@pytest.mark.parametrize("seed", SEEDS)
def test_pit_store_query_matches_brute_force(seed, registry):
    rng = np.random.default_rng(seed)
    store, all_recs = PITStore(registry), []
    for k in range(int(rng.integers(1, 6))):                      # 관측값 k마다 원본 + 정정 몇 개
        prev, t = None, T0 + timedelta(minutes=int(rng.integers(0, 1000)))
        for j in range(int(rng.integers(1, 4))):
            r = PITRecord(entity_id="E", series_id="S", metric_id=f"m{k}", available_at_utc=t.to_pydatetime(),
                          ingested_at_utc=(t + timedelta(minutes=1)).to_pydatetime(), revision_id=f"m{k}-r{j}",
                          value=float(rng.normal()), unit="x", source_provider="P", raw_payload_hash="h",
                          license_contract_id=str(rng.choice(["kr_price", "store_only"])), supersedes_revision_id=prev)
            store.append(r)
            all_recs.append(r)
            prev, t = r.revision_id, t + timedelta(minutes=int(rng.integers(0, 500)))

    for as_of in _random_times(rng, 10, minutes=3000):
        for purpose in (Purpose.STORAGE, Purpose.ML_TRAINING):
            got = store.query_as_of(as_of.to_pydatetime(), purpose)
            assert (got["available_at_utc"] <= as_of).all()
            expect = {}
            for r in all_recs:
                if r.available_at_utc <= as_of and registry.is_allowed(r.license_contract_id, purpose):
                    cur = expect.get(r.metric_id)
                    if cur is None or (r.available_at_utc, r.ingested_at_utc) > (cur.available_at_utc, cur.ingested_at_utc):
                        expect[r.metric_id] = r
            assert sorted(got["revision_id"]) == sorted(r.revision_id for r in expect.values())
