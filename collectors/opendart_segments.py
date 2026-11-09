"""XBRL에서 사업부문·지역별 숫자를 골라내고 PIT 레코드로 바꾼다 (가이드 5-3).

실제 DART XBRL을 점검해 확인한 구조 (2025 사업보고서, 2026-11-09):
- 거의 모든 숫자 사실에 연결·별도 차원(ConsolidatedAndSeparateFinancialStatementsAxis)이 붙는다.
- 부문은 ifrs-full:SegmentsAxis의 회사 확장 멤버다. 같은 부문(예: '조선')이 영업부문 표뿐 아니라 공사계약·수익
  분해 표에도 따로 나오고, 멤버 이름 끝에 어느 표인지가 들어 있다(…MemberOf<표 이름>TableOfMember).
  → 영업부문 표의 멤버만 고른다. 표 이름에 OperatingSegments가 없는 회사도 있어(예: 주요 사업 현황 표),
    '여러 부문이 매출과 영업이익을 함께 가진 표'를 후보로 삼고, OperatingSegments 표를 먼저 고른다.
- 영업이익 대신 다른 이익(ProfitLoss 등)만 공시하는 회사가 있다 → 역할을 segment_profit으로 따로 기록한다.
- 지역은 ifrs-full:GeographicalAreasAxis. 같은 보고서에 기업 전체 지역 정보(IFRS 8)와 수익 분해(IFRS 15) 표가
  섞여 있어, (연결 구분·통합항목·개념)이 같은 묶음 가운데 하나만 고른다.
부문 표가 없으면(단일 부문 회사 등) 추정하지 않고 상태로 남긴다.
"""
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Callable, Optional

from collectors.opendart import PROVIDER, VIEWER_URL, available_at
from collectors.opendart_xbrl import (CONSOLIDATION_AXIS, GEO_AXIS, SEGMENT_AXIS, SEGMENT_ITEMS_AXIS, Fact, XbrlDoc)
from core.pit_schema import PITRecord

SERIES_SEGMENTS = "dart_segments"
EXTRACTOR_VERSION = "segments-0.1"
CONSOLIDATED, SEPARATE = "ifrs-full:ConsolidatedMember", "ifrs-full:SeparateMember"
OPERATING_ITEMS = "ifrs-full:OperatingSegmentsMember"

REVENUE = ("ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers")
OPERATING_INCOME = ("dart:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities")
SEGMENT_PROFIT = ("ifrs-full:ProfitLoss", "ifrs-full:ProfitLossBeforeTax")
# 합계·조정 멤버는 부문이 아니다
TOTAL_MEMBERS = {"ifrs-full:OperatingSegmentsMember", "ifrs-full:ReportableSegmentsMember",
                 "ifrs-full:MaterialReconcilingItemsMember", "ifrs-full:EliminationOfIntersegmentAmountsMember",
                 "ifrs-full:UnallocatedAmountsMember"}
EXCLUDED_TABLE = re.compile(r"Contract|Construction|ProductionHour|CashFlow|Elimination|Impairment|Asset", re.I)


def member_table(member: str) -> Optional[str]:
    """'…MemberOf<표>TableOfMember' → '<표>Table'. 표 정보가 없는 표준 멤버는 None."""
    local = member.split(":", 1)[-1]
    end = local.rfind("TableOfMember")
    if end < 0:
        return None
    start = local.rfind("MemberOf", 0, end)
    return local[start + len("MemberOf"):end] + "Table" if start >= 0 else None


def clean_label(label: str) -> str:
    return re.sub(r"\s*\[구성요소\]\s*$", "", (label or "").strip()).strip()


def _period(doc: XbrlDoc) -> Optional[tuple]:
    """보고 기간: 끝이 가장 늦은 기간 중 가장 긴(누적) 기간."""
    dur = [f.context for f in doc.facts if f.value is not None and not f.context.instant
           and f.context.start and f.context.end]
    if not dur:
        return None
    end = max(c.end for c in dur)
    return min(c.start for c in dur if c.end == end), end


@dataclass(frozen=True)
class SegmentValue:
    kind: str                 # segment | geography
    key: str                  # 사람이 읽는 키 (정리한 한국어 레이블)
    member: str
    table: Optional[str]
    role: str                 # revenue | operating_income | segment_profit
    concept: str
    value: float
    consolidation: str        # CONSOLIDATED | SEPARATE
    start: date
    end: date


@dataclass
class SegmentExtraction:
    values: list = field(default_factory=list)
    segment_status: str = "NO_SEGMENT_TABLE"     # OK | PROFIT_NOT_OPERATING | NO_SEGMENT_TABLE | NO_PERIOD
    geography_status: str = "NO_GEOGRAPHY"       # OK | NO_GEOGRAPHY
    segment_table: Optional[str] = None
    geography_group: Optional[str] = None
    notes: list = field(default_factory=list)
    period_start: Optional[date] = None
    period_end: Optional[date] = None


def _split(f: Fact):
    cons, other = None, {}
    for a, m in f.context.dims:
        if a == CONSOLIDATION_AXIS:
            cons = m
        else:
            other[a] = m
    return cons, other


def _pick(vals: dict, concepts: tuple) -> Optional[tuple]:
    for c in concepts:
        if c in vals:
            return c, vals[c]
    return None


def _cons_name(member: Optional[str]) -> Optional[str]:
    return {CONSOLIDATED: "CONSOLIDATED", SEPARATE: "SEPARATE", None: "CONSOLIDATED"}.get(member)


def extract_segments(doc: XbrlDoc) -> SegmentExtraction:
    out = SegmentExtraction()
    period = _period(doc)
    if period is None:
        out.segment_status, out.geography_status = "NO_PERIOD", "NO_GEOGRAPHY"
        return out
    start, end = period
    out.period_start, out.period_end = start, end
    cur = [f for f in doc.facts if f.value is not None and not f.context.instant
           and f.context.start == start and f.context.end == end]

    # ---- 부문: 연결(없으면 별도) · SegmentsAxis 하나 (+ 통합항목이 영업부문) ----
    seg = defaultdict(dict)       # (cons, member) → {concept: value}
    for f in cur:
        cons, other = _split(f)
        if SEGMENT_AXIS not in other or set(other) - {SEGMENT_AXIS, SEGMENT_ITEMS_AXIS}:
            continue
        if other.get(SEGMENT_ITEMS_AXIS, OPERATING_ITEMS) != OPERATING_ITEMS:
            continue
        member = other[SEGMENT_AXIS]
        if member in TOTAL_MEMBERS:
            continue
        seg[(cons, member)].setdefault(f.concept, f.value)

    for cons in (CONSOLIDATED, None, SEPARATE):
        tables = defaultdict(list)
        for (c, member), vals in seg.items():
            if c != cons:
                continue
            table = member_table(member)
            if table is not None and EXCLUDED_TABLE.search(table):
                continue
            tables[table].append((member, vals))
        # 후보: 표 정보가 있는 표 가운데 매출을 가진 부문이 2개 이상
        def score(item):
            table, rows = item
            n_rev = sum(1 for _, v in rows if _pick(v, REVENUE))
            n_oi = sum(1 for _, v in rows if _pick(v, OPERATING_INCOME))
            return ("OperatingSegment" in table, n_oi >= 2, n_rev)
        candidates = [(t, rows) for t, rows in tables.items()
                      if t is not None and sum(1 for _, v in rows if _pick(v, REVENUE)) >= 2]
        if not candidates:
            continue
        table, rows = max(candidates, key=score)
        # 표 정보가 없는 표준 멤버(예: 기타부문 AllOtherSegmentsMember)는 매출이 있으면 함께 넣는다
        rows = rows + [(m, v) for m, v in tables.get(None, []) if _pick(v, REVENUE)]
        has_oi = sum(1 for _, v in rows if _pick(v, OPERATING_INCOME)) >= 1
        for member, vals in rows:
            key = clean_label(doc.label(member))
            rev = _pick(vals, REVENUE)
            if rev:
                out.values.append(SegmentValue("segment", key, member, member_table(member), "revenue", rev[0],
                                               rev[1], _cons_name(cons), start, end))
            prof = _pick(vals, OPERATING_INCOME) if has_oi else _pick(vals, SEGMENT_PROFIT)
            if prof:
                role = "operating_income" if has_oi else "segment_profit"
                out.values.append(SegmentValue("segment", key, member, member_table(member), role, prof[0],
                                               prof[1], _cons_name(cons), start, end))
        out.segment_status = "OK" if has_oi else "PROFIT_NOT_OPERATING"
        out.segment_table = table
        if cons != CONSOLIDATED:
            out.notes.append(f"연결 부문 표가 없어 {_cons_name(cons)} 기준 사용")
        break

    # ---- 지역: (연결 구분, 통합항목, 개념)이 같은 묶음 하나 ----
    groups = defaultdict(dict)
    for f in cur:
        cons, other = _split(f)
        if GEO_AXIS not in other or set(other) - {GEO_AXIS, SEGMENT_ITEMS_AXIS} or f.concept not in REVENUE:
            continue
        if other.get(SEGMENT_ITEMS_AXIS, OPERATING_ITEMS) != OPERATING_ITEMS:
            continue
        g = (cons, other.get(SEGMENT_ITEMS_AXIS), f.concept)
        groups[g].setdefault(other[GEO_AXIS], f.value)
    ok = [(g, m) for g, m in groups.items() if len(m) >= 2]
    if ok:
        # 연결 우선, 그다음 매출 합이 큰(포괄 범위가 넓은) 묶음
        (cons, items, concept), members = max(ok, key=lambda x: (x[0][0] == CONSOLIDATED, sum(x[1].values())))
        for member, value in members.items():
            out.values.append(SegmentValue("geography", clean_label(doc.label(member)), member, member_table(member),
                                           "revenue", concept, value, _cons_name(cons), start, end))
        out.geography_status = "OK"
        out.geography_group = f"{_cons_name(cons)}|{items or '-'}|{concept}"
    return out


# ---------------- PIT 레코드 ----------------

def _metric(v: SegmentValue, seen: dict) -> str:
    base = f"{v.consolidation}|{v.kind}|{v.key}|{v.role}"
    if base in seen and seen[base] != v.member:          # 같은 레이블의 다른 멤버: 멤버로 구분
        return f"{base}#{hashlib.sha256(v.member.encode()).hexdigest()[:6]}"
    seen.setdefault(base, v.member)
    return base


def segment_records(ext: SegmentExtraction, *, corp_code: str, rcept_no: str, rcept_dt: date, payload_sha256: str,
                    contract_id: str, ingested_at_utc: datetime,
                    latest: Callable[[tuple], Optional[PITRecord]]) -> tuple[list, int]:
    """추출 결과 → PIT 레코드. 상태 레코드(부문 표 유무)를 함께 남겨 '없음'과 '아직 안 받음'을 구분한다.
    정정 보고서는 값이 바뀐 것만 이전 버전을 가리키는 새 revision으로 넣는다. 반환: (레코드, 값이 같아 건너뛴 수)."""
    avail = available_at(rcept_dt, ingested_at_utc)
    common = dict(entity_id=corp_code, series_id=SERIES_SEGMENTS, available_at_utc=avail, ingested_at_utc=ingested_at_utc,
                  source_provider=PROVIDER, raw_payload_hash=payload_sha256, license_contract_id=contract_id,
                  vintage_date=rcept_dt, source_document_id=rcept_no, source_url=VIEWER_URL.format(rcept_no))
    out, same, seen = [], 0, {}
    status = PITRecord(metric_id="extraction_status", revision_id=f"{rcept_no}|extraction_status",
                       value=json.dumps({"segment": ext.segment_status, "geography": ext.geography_status},
                                        sort_keys=True), unit="json", observation_period_end=ext.period_end,
                       attributes=json.dumps({"extractor": EXTRACTOR_VERSION, "segment_table": ext.segment_table,
                                              "geography_group": ext.geography_group, "notes": ext.notes},
                                             ensure_ascii=False, sort_keys=True), **common)
    for rec in [status] + [
        PITRecord(metric_id=(m := _metric(v, seen)), revision_id=f"{rcept_no}|{m}", value=v.value, unit="KRW",
                  currency="KRW", observation_period_start=v.start, observation_period_end=v.end,
                  attributes=json.dumps({"label": v.key, "member": v.member, "table": v.table, "concept": v.concept},
                                        ensure_ascii=False, sort_keys=True), **common)
            for v in ext.values]:
        prev = latest(rec.observation_key)
        if prev is not None and prev.source_document_id != rcept_no:
            if (prev.source_document_id or "") > rcept_no:
                continue
            if prev.value == rec.value:
                same += 1
                continue
            rec = replace(rec, supersedes_revision_id=prev.revision_id)
        out.append(rec)
    return out, same
