"""사업부문별 글로벌 노출도 (가이드 5-3).

기업 → 사업부문 → 산업의 2단계로 연결한다. 비중은 as_of 시점에 이용 가능했던 공시(PIT)에서만 가져오고, 부문→산업
연결은 버전이 있는 레이블 규칙(config/segment_taxonomy.json)으로 한다. 회사별 비중을 코드에 고정하지 않는다.

- 매출 가중과 이익 가중 노출도를 둘 다 낸다. 이익이 음수인 부문이 있으면 이익 가중은 LIMITED.
- 부문 표가 없으면(단일 부문 회사 등) 추정하지 않고 INSUFFICIENT.
- 부문마다 연결 규칙·신뢰도·드라이버 데이터 상태를 따로 보여 준다 (드라이버 데이터는 아직 수집 전).
- 부문 매출에는 부문 간 내부 매출이 섞여 있을 수 있어, 비중은 '부문 매출 합' 대비로 계산하고 그 사실을 기록한다.
"""
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from collectors.opendart_segments import SERIES_SEGMENTS
from core.coverage import CoverageStatus
from core.data_contracts import Purpose
from core.pit_schema import PITStore

TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "segment_taxonomy.json"
MAX_AGE = timedelta(days=550)            # coverage.DEFAULT_REQUIREMENTS의 segment_disclosure와 같은 기준
UNMAPPED = "UNMAPPED"


@dataclass(frozen=True)
class Taxonomy:
    version: str
    industries: tuple          # (id, name, keywords, drivers)
    regions: tuple             # (id, keywords)
    unmapped: tuple

    @classmethod
    def load(cls, path=TAXONOMY_PATH) -> "Taxonomy":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["version"], tuple((i["id"], i["name"], tuple(i["keywords"]), tuple(i.get("drivers", ())))
                                       for i in d["industries"]),
                   tuple((r["id"], tuple(r["keywords"])) for r in d["regions"]), tuple(d.get("unmapped_keywords", ())))

    @staticmethod
    def _find(label: str, keyword: str) -> int:
        if keyword.isascii():
            m = re.search(rf"(?<![A-Za-z]){re.escape(keyword)}(?![A-Za-z])", label, re.I)
            return m.start() if m else -1
        return label.find(keyword)

    def _best(self, label: str, rules, keywords_of) -> Optional[tuple]:
        """레이블에서 가장 앞에 나온(같으면 더 긴) 키워드의 규칙. 반환 (규칙, 키워드) 또는 None."""
        best = None
        for rule in rules:
            for kw in keywords_of(rule):
                pos = self._find(label, kw)
                if pos >= 0 and (best is None or (pos, -len(kw)) < best[0]):
                    best = ((pos, -len(kw)), rule, kw)
        return best[1:] if best else None

    def classify(self, label: str) -> tuple:
        """부문 레이블 → (산업 ID, 산업 이름, 맞은 키워드, 드라이버 후보)."""
        if any(label.strip().startswith(k) for k in self.unmapped):
            return UNMAPPED, "분류 안 됨", None, ()
        best = self._best(label, self.industries, lambda r: r[2])
        if best is None:
            return UNMAPPED, "분류 안 됨", None, ()
        (iid, name, _kws, drivers), kw = best
        return iid, name, kw, drivers

    def region(self, label: str) -> str:
        best = self._best(label, self.regions, lambda r: r[1])
        return best[0][0] if best else "OTHER"


@dataclass(frozen=True)
class SegmentRow:
    key: str
    industry: str
    industry_name: str
    matched_keyword: Optional[str]
    revenue: Optional[float]
    profit: Optional[float]
    revenue_share: Optional[float]
    profit_share: Optional[float]
    confidence: str                      # MEDIUM(레이블 규칙으로 연결) | LOW(연결 안 됨)
    driver_candidates: tuple
    driver_coverage: str = "NOT_COLLECTED"   # 드라이버 데이터는 아직 수집 전 (계약 등록 후 연결)


@dataclass(frozen=True)
class ExposureReport:
    entity_id: str
    as_of: datetime
    status: CoverageStatus
    reasons: tuple
    taxonomy_version: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    consolidation: Optional[str] = None
    source_document_ids: tuple = ()
    available_at_utc: Optional[datetime] = None
    profit_role: Optional[str] = None            # operating_income | segment_profit
    profit_weight_status: str = "UNAVAILABLE"    # OK | LIMITED | UNAVAILABLE
    segments: tuple = ()
    industry_revenue_weights: dict = field(default_factory=dict)
    industry_profit_weights: dict = field(default_factory=dict)
    geography_weights: dict = field(default_factory=dict)       # 공시 레이블 → 비중
    region_weights: dict = field(default_factory=dict)          # 정규화 지역 → 비중
    share_basis: str = "부문 매출 합 대비 (부문 간 내부 매출 포함 가능)"


def _parse_metric(metric_id: str):
    """'연결구분|종류|키|역할[#구분자]' → (연결구분, 종류, 키, 역할). 같은 레이블의 다른 멤버는 키에 구분자를 붙여 따로 센다."""
    parts = metric_id.split("|")
    if len(parts) < 4:
        return None
    role, _, tag = parts[-1].partition("#")
    key = "|".join(parts[2:-1]) + (f"#{tag}" if tag else "")
    return parts[0], parts[1], key, role


def compute_exposure(store: PITStore, entity_id: str, as_of: datetime, taxonomy: Optional[Taxonomy] = None,
                     purpose: Purpose = Purpose.DERIVED_USE) -> ExposureReport:
    tax = taxonomy or Taxonomy.load()
    df = store.query_as_of(as_of, purpose, entity_id=entity_id, series_id=SERIES_SEGMENTS)
    if df.empty:
        return ExposureReport(entity_id, as_of, CoverageStatus.INSUFFICIENT,
                              ("as_of 시점에 이용 가능한 부문 공시가 없음",), tax.version)
    status_rows = df[df.metric_id == "extraction_status"].copy()
    if status_rows.empty:
        return ExposureReport(entity_id, as_of, CoverageStatus.INSUFFICIENT, ("추출 상태 기록이 없음",), tax.version)
    latest = status_rows.sort_values(["observation_period_end", "available_at_utc"]).iloc[-1]
    period_end = latest.observation_period_end
    ext_status = json.loads(latest.value)
    rows = df[(df.observation_period_end == period_end) & (df.metric_id != "extraction_status")]
    reasons, status = [], CoverageStatus.SUFFICIENT
    avail = rows.available_at_utc.max() if not rows.empty else latest.available_at_utc
    if as_of - avail.to_pydatetime() > MAX_AGE:
        status = CoverageStatus.STALE
        reasons.append(f"마지막 부문 공시가 {MAX_AGE.days}일보다 오래됨 ({period_end})")

    parsed = defaultdict(dict)            # (cons, kind) → {key: {role: value}}
    for r in rows.itertuples():
        p = _parse_metric(r.metric_id)
        if p is None:
            continue
        cons, kind, key, role = p
        parsed[(cons, kind)].setdefault(key, {})[role] = float(r.value)

    cons = "CONSOLIDATED" if parsed.get(("CONSOLIDATED", "segment")) else \
        "SEPARATE" if parsed.get(("SEPARATE", "segment")) else None
    common = dict(period_start=str(rows.observation_period_start.min()) if not rows.empty else None,
                  period_end=str(period_end), consolidation=cons,
                  source_document_ids=tuple(sorted(set(rows.source_document_id) | {latest.source_document_id})),
                  available_at_utc=avail.to_pydatetime() if hasattr(avail, "to_pydatetime") else avail)

    # 지역
    geo = parsed.get(("CONSOLIDATED", "geography")) or parsed.get(("SEPARATE", "geography")) or {}
    geo_rev = {k: v["revenue"] for k, v in geo.items() if v.get("revenue") is not None and v["revenue"] > 0}
    gsum = sum(geo_rev.values())
    geography = {k: v / gsum for k, v in geo_rev.items()} if gsum > 0 else {}
    regions = defaultdict(float)
    for k, w in geography.items():
        regions[tax.region(k)] += w

    if cons is None:
        reasons.append(f"부문 정보 없음 (추출 상태 {ext_status.get('segment')}) — 단일 부문 회사일 수 있어 추정하지 않음")
        return ExposureReport(entity_id, as_of, CoverageStatus.INSUFFICIENT if status != CoverageStatus.STALE else status,
                              tuple(reasons), tax.version, geography_weights=geography, region_weights=dict(regions),
                              **common)
    if cons == "SEPARATE":
        reasons.append("연결 부문 정보가 없어 별도 기준 사용")
        status = max(status, CoverageStatus.LIMITED, key=_severity)

    seg = parsed[(cons, "segment")]
    revs = {k: v.get("revenue") for k, v in seg.items()}
    profit_role = "operating_income" if any("operating_income" in v for v in seg.values()) else \
        "segment_profit" if any("segment_profit" in v for v in seg.values()) else None
    profits = {k: v.get(profit_role) for k, v in seg.items()} if profit_role else {}
    rsum = sum(v for v in revs.values() if v is not None and v > 0)
    psum = sum(v for v in profits.values() if v is not None)
    if any(v is not None and v < 0 for v in revs.values()):
        reasons.append("음수 매출 부문은 비중 계산에서 뺌")

    pw_status = "UNAVAILABLE"
    if profit_role and all(profits.get(k) is not None for k in seg) and psum > 0:
        pw_status = "LIMITED" if any(v < 0 for v in profits.values()) else "OK"
    if pw_status == "LIMITED":
        reasons.append("이익이 음수인 부문이 있어 이익 가중 노출도는 제한적")
    if profit_role == "segment_profit":
        reasons.append("영업이익이 아니라 부문 이익(당기순이익 등) 기준")
    if pw_status != "OK":
        status = max(status, CoverageStatus.LIMITED, key=_severity)

    out_rows, ind_rev, ind_prof = [], defaultdict(float), defaultdict(float)
    for key in sorted(seg, key=lambda k: -(revs.get(k) or 0)):
        iid, name, kw, drivers = tax.classify(key)
        rev = revs.get(key)
        rshare = rev / rsum if rev is not None and rev > 0 and rsum > 0 else None
        pshare = profits[key] / psum if pw_status in ("OK", "LIMITED") else None
        out_rows.append(SegmentRow(key, iid, name, kw, rev, profits.get(key), rshare, pshare,
                                   "MEDIUM" if iid != UNMAPPED else "LOW", drivers))
        if rshare is not None:
            ind_rev[iid] += rshare
        if pshare is not None:
            ind_prof[iid] += pshare
    unmapped = ind_rev.get(UNMAPPED, 0.0)
    if unmapped >= 0.2:
        reasons.append(f"산업에 연결되지 않은 부문의 매출 비중 {unmapped:.0%}")
        status = max(status, CoverageStatus.LIMITED, key=_severity)
    reasons.append("부문별 글로벌 드라이버 데이터는 아직 수집 전 (노출 비중만 제공)")
    return ExposureReport(entity_id, as_of, status, tuple(reasons), tax.version, profit_role=profit_role,
                          profit_weight_status=pw_status, segments=tuple(out_rows),
                          industry_revenue_weights=dict(ind_rev), industry_profit_weights=dict(ind_prof),
                          geography_weights=geography, region_weights=dict(regions), **common)


_ORDER = {CoverageStatus.SUFFICIENT: 0, CoverageStatus.LIMITED: 1, CoverageStatus.STALE: 2, CoverageStatus.INSUFFICIENT: 3}


def _severity(s: CoverageStatus) -> int:
    return _ORDER[s]
