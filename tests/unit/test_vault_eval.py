"""Vault 평가: 확정 문서 해시 확인, 한 번만, 순차 검정, 개발/Vault 판단일. 합성 데이터."""
import hashlib
from datetime import date

import numpy as np
import pandas as pd
import pytest

from evaluation import vault_eval
from evaluation.experiment_registry import ExperimentRegistry
from storage.visibility import PRIVATE_MARKER
from tests.unit.test_walkforward import synth_panel


def doc(tmp_path, status="**상태: 확정 (테스트)**"):
    p = tmp_path / "prereg.md"
    p.write_text(f"# t\n\n> {status}\n", encoding="utf-8")
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def test_real_prereg_matches_recorded_hash():
    vault_eval.check_prereg()                     # 저장소의 확정 문서가 바뀌지 않았다


def test_refuses_changed_or_unconfirmed_doc(tmp_path):
    p, sha = doc(tmp_path)
    vault_eval.check_prereg(p, sha)
    p.write_text(p.read_text() + "x")
    with pytest.raises(PermissionError, match="바뀌었다"):
        vault_eval.check_prereg(p, sha)
    q, sha2 = doc(tmp_path, status="**상태: 초안**")
    with pytest.raises(PermissionError, match="확정 상태"):
        vault_eval.check_prereg(q, sha2)


def test_decision_dates_stop_before_missing_labels():
    p = synth_panel(n_codes=2, start=date(2019, 1, 2), end=date(2019, 12, 30))
    dev, v = vault_eval.decision_dates(p, date(2019, 7, 1))
    s = pd.DatetimeIndex(sorted(p["date"].unique()))
    assert dev[0] == s[0] and (dev < pd.Timestamp("2019-07-01")).all()
    assert v[0] == s[s >= pd.Timestamp("2019-07-01")][0]
    assert s.get_loc(v[-1]) + vault_eval.H <= len(s) - 1          # 정답(D+21 시가)이 패널 안에 있음


def test_runs_once_with_sequential_judgement(tmp_path):
    (tmp_path / PRIVATE_MARKER).write_text("private\n")
    p, sha = doc(tmp_path)
    panel = synth_panel(n_codes=120)
    loader = lambda d, s, e, **kw: panel
    out = vault_eval.run(tmp_path, loader=loader, vault_start=date(2019, 7, 1), prereg_path=p, prereg_sha=sha,
                         log=lambda s: None)
    r = out["vault"]["results"]
    assert set(r) == {"H1", "H2"} and r["H1"]["model"] == "ens" and r["H2"]["model"] == "mlp"
    assert len(out["vault"]["folds"]) == 2 and r["H1"]["judgement"]["judged"] is True
    assert r["H2"]["judgement"]["judged"] == r["H1"]["judgement"]["pass"]           # H1이 통과해야 H2 판정
    assert r["H1"]["judgement"]["ic_mean"] > 0.03                                   # 심어 둔 신호
    assert "검증된 신호" in vault_eval.report(out) or "탐색 결과" in vault_eval.report(out)
    assert ExperimentRegistry(tmp_path).count_trials("RET_21D") == 0                # 시행 수에 넣지 않음
    with pytest.raises(PermissionError, match="이미"):
        vault_eval.run(tmp_path, loader=loader, vault_start=date(2019, 7, 1), prereg_path=p, prereg_sha=sha)


def test_bootstrap_p_is_small_for_clear_positive_and_large_for_zero():
    rng = np.random.default_rng(0)
    assert vault_eval.bootstrap_p_one_sided(0.03 + rng.normal(0, 0.03, 24)) < 0.01
    assert vault_eval.bootstrap_p_one_sided(rng.normal(0, 0.03, 24) - 0.01) > 0.5
