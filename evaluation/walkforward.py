"""Purged Walk-forward 분할과 잠긴 최종 OOS 구간 (가이드 7-5, 7-12).

- 확장 창(expanding): 학습은 처음부터, 검증은 test_months 단위로 앞으로 민다.
- **Purge:** 학습 표본은 정답을 쓸 수 있는 시각(label_available_at)이 검증 구간 첫 판단 시각보다 앞선 것만 쓴다.
  보유 기간이 검증 구간에 걸치는 표본(정답이 검증 시점에 아직 없는 것)은 자동으로 빠진다.
- **Embargo:** 그 위에 embargo_sessions만큼 더 떨어뜨린다 (피처 창이 겹치는 상관 완화).
- **Vault:** vault_start 이후 판단일은 어떤 폴드에도 넣지 않는다. Vault 평가는 사전 등록한 후보만,
  experiment_registry의 사전 등록 기록이 있을 때만 한 번 한다 (7-12).
"""
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class Fold:
    fold_id: int
    test_start: pd.Timestamp
    test_end: pd.Timestamp           # 포함
    train_label_before: pd.Timestamp  # 학습 표본의 label_available_at 상한 (미만)
    train_end: pd.Timestamp          # 학습 판단일 상한 (포함)


def make_folds(dates, *, first_test: date, vault_start: date, test_months: int = 12,
               embargo_sessions: int = 5) -> list:
    sessions = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Series(dates).unique())))
    vault = pd.Timestamp(vault_start)
    folds, start, i = [], pd.Timestamp(first_test), 0
    while start < vault:
        end = min(start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1), vault - pd.Timedelta(days=1))
        in_test = sessions[(sessions >= start) & (sessions <= end)]
        if len(in_test) == 0:
            break
        first = in_test[0]
        before = sessions[sessions < first]
        if len(before) <= embargo_sessions:
            start = end + pd.Timedelta(days=1)
            continue
        train_end = before[-1 - embargo_sessions]
        folds.append(Fold(i, first, in_test[-1], first, train_end))
        i += 1
        start = end + pd.Timedelta(days=1)
    return folds


def split(df: pd.DataFrame, fold: Fold, decision_col: str = "date", label_col: str = "label_available_at",
          decision_time_col: str = "decision_time_utc"):
    """(학습, 검증) 표본. 학습: 판단일 <= train_end 이고 정답 가용 시각 < 검증 첫 판단 시각."""
    test_mask = (df[decision_col] >= fold.test_start) & (df[decision_col] <= fold.test_end)
    first_decision = df.loc[test_mask, decision_time_col].min()
    lab = pd.to_datetime(df[label_col], utc=True)
    train_mask = (df[decision_col] <= fold.train_end) & lab.notna() & (lab < first_decision)
    return df[train_mask], df[test_mask]


def assert_no_vault(df: pd.DataFrame, vault_start: date, decision_col: str = "date") -> None:
    if (df[decision_col] >= pd.Timestamp(vault_start)).any():
        raise PermissionError(f"Vault 구간({vault_start}~)의 표본이 탐색·검증에 들어옴 — 사전 등록 없이 볼 수 없다 (7-12)")
