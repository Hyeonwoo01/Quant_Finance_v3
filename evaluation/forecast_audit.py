"""예측 기록과 정산 (Forecast Audit Trail, 가이드 7-10).

- 예측은 만든 시점에 추가만 한다 (forecasts/<YYYY-MM>.jsonl). 고치지 않는다.
- 실현값은 따로 평가 기록으로 추가한다 (evaluations/<YYYY-MM>.jsonl). 원래 예측 기록은 건드리지 않는다.
- **소급 기록 금지:** 예측을 만든 시각이 data_cutoff 다음 세션 시작(09:00 KST) 이후면 기록하지 않는다.
- **정산 (TradingAgents의 판단 기록 정산과 같은 생각):** 보유 기간이 지나 정답을 쓸 수 있게 된 예측만 정산한다.
  체결은 모두 SIMULATED(백테스터 가정)이고, 예측 정확도(타깃 실현값)와 체결 결과를 따로 적는다.
- **회고 (failure_attribution):** 크게 틀린 예측에 원인 후보를 데이터로 계산해 남긴다 — 시장 전체 움직임,
  시장 대비 초과수익, 진입 갭, 보유 중 기업행위, 가격제한폭 도달, 확률 과대평가. 회고 문장은 규칙으로 만든다.
  KRX 시세는 외부 LLM에 넣을 수 없기 때문이다 (약관 제11조 ②, 계약 llm_input_allowed=False).
- 비용(수수료·세금·슬리피지)은 비용 모델(8-3·8-4)을 정하기 전까지 비워 둔다 (return_net=None).
- 시세에서 나온 값(진입·청산 가격 등)이 들어가므로 비공개 저장소에만 쓴다.
"""
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from core.calendar_align import KR_VENUE, calendar, next_session_after
from core.data_contracts import PermissionDenied
from storage.visibility import is_private_store

UTC = timezone.utc


@dataclass(frozen=True)
class ForecastRecord:
    forecast_id: str
    created_at_utc: datetime
    decision_time_utc: datetime            # 판단 세션 D의 08:30 KST
    data_cutoff_utc: datetime              # 어느 시점까지의 정보로 판단했는가
    ticker: str
    model_id: str
    target_definition_id: str              # evaluation.targets.TARGETS
    holding_period_sessions: int
    entry_price_assumption: str = "KRX_OPEN_D"
    predicted_return: Optional[float] = None
    predicted_probability: Optional[float] = None     # 상승 확률
    prediction_interval: Optional[dict] = None
    feature_snapshot_id: Optional[str] = None
    feature_hash: Optional[str] = None
    universe_snapshot_id: Optional[str] = None
    signal_status: str = "ACTIVE"
    abstained: bool = False
    score: Optional[float] = None               # 모델 점수 (순위 모델은 수익률이 아니라 점수, 2026-11 실전 기록부터)
    rank_pct: Optional[float] = None            # 그날 유니버스 안 점수 백분위 (1 = 가장 높음)


@dataclass(frozen=True)
class ForecastEvaluation:
    forecast_id: str
    evaluated_at_utc: datetime
    fill_type: str                          # SIMULATED (증권사 체결 기록과 연결하기 전까지 전부)
    fill_status: str                        # FILLED | NOT_FILLED | NOT_TRADABLE | NO_EXIT
    evaluation_status: str                  # SETTLED | NO_LABEL
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    realized_return: Optional[float] = None         # 타깃 정의의 실현값 (정확도 평가용, 체결과 무관)
    return_gross: Optional[float] = None            # 체결됐다고 볼 수 있을 때만
    return_net: Optional[float] = None              # 비용 모델 전까지 None
    cost_breakdown: Optional[dict] = None
    label_available_at_utc: Optional[datetime] = None
    flags: dict = field(default_factory=dict)
    failure_attribution: Optional[dict] = None


def _json(obj) -> str:
    def conv(v):
        if isinstance(v, datetime):
            return v.astimezone(UTC).isoformat()
        if isinstance(v, float) and math.isnan(v):
            return None
        return v
    return json.dumps({k: conv(v) for k, v in asdict(obj).items()}, ensure_ascii=False, sort_keys=True)


def _dt(s):
    return datetime.fromisoformat(s) if isinstance(s, str) else s


def session_start_after(t_utc: datetime) -> datetime:
    """data_cutoff 다음 KRX 세션의 09:00 KST (UTC). 그 뒤에 만든 예측은 실전 기록이 아니다."""
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    local = t_utc.astimezone(kst)
    cal = calendar(KR_VENUE)
    day = pd.Timestamp(local.date())
    s = cal.date_to_session(day, direction="next")
    start = pd.Timestamp.combine(s.date(), datetime.min.time().replace(hour=9)).tz_localize(kst)
    if start <= pd.Timestamp(local):
        nxt = next_session_after(s.date())
        start = pd.Timestamp.combine(nxt.date(), datetime.min.time().replace(hour=9)).tz_localize(kst)
    return start.tz_convert("UTC").to_pydatetime()


class ForecastLedger:
    def __init__(self, root):
        self.root = Path(root)

    def _guard(self):
        if not is_private_store(self.root):
            raise PermissionDenied(f"{self.root}: 비공개 저장소가 아님 — 예측·정산 기록에는 시세에서 나온 값이 들어간다")

    def _append(self, kind: str, when: datetime, line: str):
        self._guard()
        p = self.root / "forecast_audit" / kind / f"{when:%Y-%m}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _read(self, kind: str) -> list:
        d = self.root / "forecast_audit" / kind
        rows = []
        for p in sorted(d.glob("*.jsonl")) if d.exists() else []:
            rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return rows

    def forecasts(self) -> list:
        out = []
        for r in self._read("forecasts"):
            for k in ("created_at_utc", "decision_time_utc", "data_cutoff_utc"):
                r[k] = _dt(r[k])
            out.append(ForecastRecord(**r))
        return out

    def evaluations(self) -> list:
        return self._read("evaluations")

    def record(self, rec: ForecastRecord) -> None:
        if rec.created_at_utc >= session_start_after(rec.data_cutoff_utc):
            raise ValueError(f"{rec.forecast_id}: data_cutoff 다음 세션이 시작된 뒤에 만든 예측 — 소급 기록 금지 (7-10)")
        if rec.data_cutoff_utc > rec.decision_time_utc:
            raise ValueError(f"{rec.forecast_id}: 판단 시각보다 늦은 정보로 만든 예측")
        if any(f.forecast_id == rec.forecast_id for f in self.forecasts()):
            raise ValueError(f"이미 있는 forecast_id: {rec.forecast_id} (예측은 고치지 않는다)")
        self._append("forecasts", rec.created_at_utc, _json(rec))

    def record_many(self, recs: list) -> int:
        """같은 판단의 여러 종목 예측을 한 번에 기록한다 (기존 id는 한 번만 읽음). 규칙은 record와 같다."""
        have = {f.forecast_id for f in self.forecasts()}
        lines = []
        for rec in recs:
            if rec.created_at_utc >= session_start_after(rec.data_cutoff_utc):
                raise ValueError(f"{rec.forecast_id}: data_cutoff 다음 세션이 시작된 뒤에 만든 예측 — 소급 기록 금지 (7-10)")
            if rec.data_cutoff_utc > rec.decision_time_utc:
                raise ValueError(f"{rec.forecast_id}: 판단 시각보다 늦은 정보로 만든 예측")
            if rec.forecast_id in have:
                raise ValueError(f"이미 있는 forecast_id: {rec.forecast_id} (예측은 고치지 않는다)")
            have.add(rec.forecast_id)
            lines.append(_json(rec))
        if lines:
            self._guard()
            p = self.root / "forecast_audit" / "forecasts" / f"{recs[0].created_at_utc:%Y-%m}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return len(lines)

    def pending(self) -> list:
        done = {e["forecast_id"] for e in self.evaluations()}
        return [f for f in self.forecasts() if f.forecast_id not in done]

    def settle(self, targets: pd.DataFrame, now_utc: datetime, *, miss_sigma: float = 0.02,
               overconfident: float = 0.65) -> list:
        """정답을 쓸 수 있게 된 예측을 정산한다. targets: evaluation.targets.build_targets 결과 (+ 시장 평균 계산용)."""
        self._guard()
        t = targets.set_index(["code", "date", "h"])
        market = targets.groupby(["date", "h"])["ret"].mean()
        now = pd.Timestamp(now_utc)
        out = []
        for f in self.pending():
            from zoneinfo import ZoneInfo
            d = pd.Timestamp(f.decision_time_utc.astimezone(ZoneInfo("Asia/Seoul")).date())
            k = (f.ticker, d, f.holding_period_sessions)
            if k not in t.index:
                continue                                    # 아직 청산 세션 전이거나 자료 없음 → PENDING
            row = t.loc[k]
            if pd.isna(row["label_available_at"]) and not row["no_exit_row"]:
                continue
            if not pd.isna(row["label_available_at"]) and pd.Timestamp(row["label_available_at"]) > now:
                continue                                    # 정답을 아직 쓸 수 없음
            ev = self._evaluate(f, row, market.get((d, f.holding_period_sessions)), now_utc, miss_sigma, overconfident)
            self._append("evaluations", now_utc, _json(ev))
            out.append(ev)
        return out

    @staticmethod
    def _evaluate(f, row, market_ret, now_utc, miss_sigma, overconfident) -> ForecastEvaluation:
        flags = {k: bool(row[k]) for k in ("entry_no_trade", "entry_limit_up", "exit_no_trade", "exit_limit_down",
                                           "no_exit_row")}
        ret = None if pd.isna(row["ret"]) else float(row["ret"])
        if flags["no_exit_row"]:
            status, fill = "NO_LABEL", "NO_EXIT"
        else:
            status = "SETTLED" if ret is not None else "NO_LABEL"
            if flags["entry_no_trade"] or flags["exit_no_trade"]:
                fill = "NOT_TRADABLE"
            elif flags["entry_limit_up"] or flags["exit_limit_down"]:
                fill = "NOT_FILLED"                          # 보수적 체결 가정 (8-2)
            else:
                fill = "FILLED"
        attribution = None
        if ret is not None:
            attribution = attribute(f, ret, market_ret, row, miss_sigma, overconfident)
        lab = row["label_available_at"]
        return ForecastEvaluation(
            forecast_id=f.forecast_id, evaluated_at_utc=now_utc, fill_type="SIMULATED", fill_status=fill,
            evaluation_status=status, entry_price=None if pd.isna(row["entry_open"]) else float(row["entry_open"]),
            exit_price=None if pd.isna(row["exit_open"]) else float(row["exit_open"]), realized_return=ret,
            return_gross=ret if fill == "FILLED" else None, return_net=None,
            cost_breakdown={"model": "미정 (가이드 8-3·8-4)"},
            label_available_at_utc=None if pd.isna(lab) else pd.Timestamp(lab).to_pydatetime(), flags=flags,
            failure_attribution=attribution)


def attribute(f: ForecastRecord, ret: float, market_ret, row, miss_sigma: float = 0.02,
              overconfident: float = 0.65) -> Optional[dict]:
    """크게 틀린 예측이면 원인 후보를 돌려준다 (아니면 None).

    크게 틀림: 방향이 틀렸고 실현 수익률 크기가 miss_sigma × √h 이상, 또는 상승 확률을 overconfident 이상(또는
    1 − overconfident 이하)으로 냈는데 방향이 틀림."""
    h = f.holding_period_sessions
    pred_up = None
    if f.predicted_return is not None:
        pred_up = f.predicted_return > 0
    elif f.predicted_probability is not None:
        pred_up = f.predicted_probability >= 0.5
    if pred_up is None or ret == 0:
        return None
    wrong = pred_up != (ret > 0)
    big = abs(ret) >= miss_sigma * math.sqrt(h)
    p = f.predicted_probability
    confident = p is not None and (p >= overconfident or p <= 1 - overconfident)
    if not (wrong and (big or confident)):
        return None
    cands = []
    mret = None if market_ret is None or pd.isna(market_ret) else float(market_ret)
    if mret is not None and (mret > 0) == (ret > 0) and abs(mret) >= 0.5 * abs(ret):
        cands.append("시장 전체 움직임")
    if mret is not None and abs(ret - mret) >= miss_sigma * math.sqrt(h):
        cands.append("종목 고유 움직임 (시장 대비)")
    if row.get("action_in_window"):
        cands.append("보유 중 기업행위")
    if row.get("entry_gap") is not None and not pd.isna(row.get("entry_gap")) and abs(row["entry_gap"]) >= 0.03:
        cands.append("진입 시가 갭")
    if bool(row["entry_limit_up"]) or bool(row["exit_limit_down"]):
        cands.append("가격제한폭 도달")
    if confident:
        cands.append("확률 과대평가")
    note = (f"{h}세션 {'상승' if pred_up else '하락'} 예측이 빗나감. 원인 후보: " + (", ".join(cands) or "특정하지 못함")
            + ". 후보는 데이터로 계산한 것이고 인과를 확정하지 않는다.")
    return {"wrong_direction": True, "big_move": big, "overconfident": confident, "market_return": mret,
            "excess_return": None if mret is None else ret - mret, "candidates": cands, "note": note}
