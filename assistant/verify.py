"""답변의 주장 검증 (가이드 14-2): 숫자가 도구 결과에 있는지만이 아니라, 그 수치의 의미·기간·기준·출처까지 대조한다.

Agent는 submit_answer로 답과 주장 목록(claims)을 낸다. 주장마다 evidence_id(도구가 돌려준 값의 PIT revision_id)를 단다.
여기서는 이번 대화의 도구 결과로 만든 근거 색인과 대조해 주장마다 상태를 매긴다.

- VERIFIED: 근거가 이번 대화의 도구 결과에 있고, 값(표시 단위 환산 후)·단위·기간·누적 구분·연결/별도·지표가 모두 맞음
- WARNING: 근거는 있으나 일부가 맞지 않거나 확인할 수 없음 (예: 분기 값을 누적이라고 함, 별도 값을 연결이라고 함)
- ABSTAINED: 근거가 없음 (도구 결과에 없는 evidence_id, 또는 근거 없이 숫자를 씀)
본문에 있는데 주장 목록에 없는 숫자도 찾아 경고한다. 연도·날짜·접수번호처럼 값이 아닌 숫자는 뺀다.
"""
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

VERIFIED, WARNING, ABSTAINED = "VERIFIED", "WARNING", "ABSTAINED"

# 표시 단위 → (기준 단위, 배수). 기준 단위가 같아야 비교한다
UNIT_SCALES = {
    "원": ("KRW", 1), "krw": ("KRW", 1), "천원": ("KRW", 10**3), "백만원": ("KRW", 10**6), "억원": ("KRW", 10**8),
    "조원": ("KRW", 10**12),
    "usd": ("USD", 1), "달러": ("USD", 1), "천달러": ("USD", 10**3), "백만달러": ("USD", 10**6), "억달러": ("USD", 10**8),
    "kg": ("KG", 1), "톤": ("KG", 10**3), "천톤": ("KG", 10**6),
    "%": ("%", 1), "연%": ("%", 1), "퍼센트": ("%", 1),
    "mbbl": ("MBBL", 1), "천배럴": ("MBBL", 1), "백만배럴": ("MBBL", 10**3),
    "bcf": ("BCF", 1), "2020=100": ("2020=100", 1), "pt": ("2020=100", 1), "포인트": ("2020=100", 1),
}


def _norm_unit(u: Optional[str]) -> Optional[tuple]:
    if not u:
        return None
    k = u.replace(" ", "").lower()
    return UNIT_SCALES.get(k, (k, 1))


@dataclass
class Evidence:
    evidence_id: str
    tool: str
    tool_use_id: str
    row: dict


@dataclass
class ClaimCheck:
    text: str
    evidence_id: Optional[str]
    status: str
    problems: list = field(default_factory=list)
    source_url: Optional[str] = None
    attribution: Optional[str] = None
    available_at_utc: Optional[str] = None


@dataclass
class Verification:
    status: str                     # 답 전체: VERIFIED · WARNING · ABSTAINED
    claims: list
    unclaimed_numbers: list
    notes: list

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceIndex:
    """이번 대화의 도구 결과에 나온 모든 evidence_id → 그 행."""

    def __init__(self):
        self.items: dict[str, Evidence] = {}

    def add_tool_result(self, tool: str, tool_use_id: str, payload: Any) -> int:
        n = 0
        stack = [payload]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("evidence_id"), str):
                    self.items.setdefault(x["evidence_id"], Evidence(x["evidence_id"], tool, tool_use_id, x))
                    n += 1
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        return n

    def __contains__(self, k):
        return k in self.items

    def __len__(self):
        return len(self.items)


def _decimals(v) -> int:
    """주장 값이 표시된 자릿수 (반올림·버림 허용 폭을 정하려고)."""
    try:
        exp = Decimal(str(v)).normalize().as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exp)


def _value_problem(claim: dict, row: dict) -> Optional[str]:
    cv, rv = claim.get("value"), row.get("value")
    if cv is None:
        return None                                  # 값 없는 주장(예: 공시가 있었다)은 근거 존재만 본다
    if not isinstance(rv, (int, float)) or isinstance(rv, bool):
        return f"근거 값이 숫자가 아님 ({rv!r})"
    try:
        cvf = float(cv)
    except (TypeError, ValueError):
        return f"주장 값이 숫자가 아님 ({cv!r})"
    cu, ru = _norm_unit(claim.get("unit")), _norm_unit(row.get("unit"))
    if cu is None:
        return "주장에 단위가 없음"
    if ru is not None and cu[0] != ru[0]:
        return f"단위 불일치: 주장 {claim.get('unit')} / 근거 {row.get('unit')}"
    scale = cu[1]
    shown = rv / scale                                # 근거 값을 주장의 표시 단위로
    step = 10 ** (-_decimals(cv))                     # 표시된 마지막 자리 1단위까지 (반올림·버림)
    if abs(shown - cvf) >= step and abs(shown - cvf) > 1e-9 * max(1.0, abs(shown)):
        return f"값 불일치: 주장 {cv} {claim.get('unit')} / 근거 {rv} {row.get('unit')} (= {shown:,.4g} {claim.get('unit')})"
    return None


def _meaning_problems(claim: dict, row: dict) -> list:
    p = []
    for key, label in (("period_end", "기간"), ("basis", "당기/누적 구분"), ("consolidation", "연결/별도")):
        c = claim.get(key)
        if c and row.get(key) and str(c) != str(row[key]):
            p.append(f"{label} 불일치: 주장 {c} / 근거 {row[key]}")
    metric = (claim.get("metric") or "").replace(" ", "")
    if metric:
        names = [str(row.get(k) or "").replace(" ", "") for k in ("account_name", "account_id", "label", "report_nm")]
        if not any(metric in n or (n and n in metric) for n in names):
            p.append(f"지표 불일치 가능: 주장 '{claim.get('metric')}' / 근거 {[n for n in names if n]}")
    return p


_NUM = re.compile(r"(?<![\w.])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])[-+]?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> list:
    """본문의 값 숫자 (연도·날짜·접수번호·코드·순번은 뺀다)."""
    out = []
    for m in _NUM.finditer(text or ""):
        s = m.group(0)
        after = text[m.end():m.end() + 2]
        before = text[max(0, m.start() - 1):m.start()]
        raw = s.replace(",", "").lstrip("+-")
        if after[:1] in ("년", "월", "일", "분", "기", "개", "건", "위", ".", "-", "/") and "." not in raw:
            continue                                   # 2026년, 3분기, 12건, 1위, 날짜
        if before in ("-", "/", "Q", "H"):
            continue
        if len(raw.split(".")[0]) >= 8 and "," not in s:
            continue                                   # 접수번호·고유번호
        if len(raw) == 6 and raw.isdigit() and "," not in s:
            continue                                   # 종목코드
        if raw.isdigit() and 1900 <= int(raw) <= 2100:
            continue                                   # 연도
        out.append(s)
    return out


def verify_answer(submission: dict, index: EvidenceIndex) -> Verification:
    notes, checks = [], []
    claims = submission.get("claims") or []
    if submission.get("status") == ABSTAINED:
        if claims:
            notes.append("답을 보류하면서 주장을 냄 — 주장은 검증하지만 답은 보류로 둔다")
    for c in claims:
        eid = c.get("evidence_id")
        if not eid or eid not in index:
            checks.append(ClaimCheck(c.get("text", ""), eid, ABSTAINED,
                                     ["근거 없음: 이번 대화의 도구 결과에 이 evidence_id가 없음"]))
            continue
        ev = index.items[eid]
        problems = [x for x in [_value_problem(c, ev.row)] if x] + _meaning_problems(c, ev.row)
        checks.append(ClaimCheck(c.get("text", ""), eid, WARNING if problems else VERIFIED, problems,
                                 ev.row.get("source_url"), ev.row.get("attribution"), ev.row.get("available_at_utc")))
    claimed = set()
    for c in claims:
        for s in _numbers_in(f"{c.get('text', '')} {c.get('value', '')}"):
            claimed.add(s.replace(",", "").lstrip("+"))
    unclaimed = [s for s in _numbers_in(submission.get("answer", "")) if s.replace(",", "").lstrip("+") not in claimed]
    if submission.get("status") == ABSTAINED:
        overall = ABSTAINED
    elif not checks:
        overall = WARNING if unclaimed else VERIFIED
        if not claims:
            notes.append("주장 목록이 비어 있음")
    elif any(x.status == ABSTAINED for x in checks) or any(x.status == WARNING for x in checks) or unclaimed:
        overall = WARNING
    else:
        overall = VERIFIED
    if unclaimed:
        notes.append(f"근거가 연결되지 않은 숫자: {unclaimed}")
    return Verification(overall, [asdict(x) for x in checks], unclaimed, notes)


def render(submission: dict, v: Verification) -> str:
    """사람이 읽는 답: 본문 + 주장별 검증 상태 + 출처."""
    mark = {VERIFIED: "✅", WARNING: "⚠️", ABSTAINED: "⛔"}
    lines = [f"**검증 상태: {mark[v.status]} {v.status}**", "", submission.get("answer", "").strip()]
    if submission.get("status") == ABSTAINED and submission.get("abstain_reason"):
        lines += ["", f"답 보류 이유: {submission['abstain_reason']}"]
    if submission.get("counter_evidence"):
        lines += ["", f"반대 근거·한계: {submission['counter_evidence']}"]
    if v.claims:
        lines += ["", "| 주장 | 상태 | 근거 |", "|---|---|---|"]
        cell = lambda x: str(x).replace("|", "\\|").replace("\n", " ")     # 표 칸 안의 | 는 이스케이프
        for c in v.claims:
            eid = cell(c.get("evidence_id") or "—")
            src = f"[{eid}]({c['source_url']})" if c.get("source_url") else eid
            note = f" ({'; '.join(c['problems'])})" if c["problems"] else ""
            lines.append(f"| {cell(c['text'] + note)} | {mark[c['status']]} {c['status']} | {src} |")
    attributions = sorted({c["attribution"] for c in v.claims if c.get("attribution")})
    if attributions:
        lines += ["", " · ".join(attributions)]
    for n in v.notes:
        lines.append(f"\n> {n}")
    return "\n".join(lines)


def submission_from_json(text: str) -> dict:
    return json.loads(text)
