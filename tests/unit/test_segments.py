"""사업부문·지역 추출과 노출도 (가이드 5-3). 합성 XBRL은 실제 DART XBRL 점검에서 본 구조를 본떴다 (값은 꾸민 숫자)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from collectors.opendart_segments import (SERIES_SEGMENTS, extract_segments, member_table, segment_records)
from collectors.opendart_xbrl import parse_xbrl_zip
from core.coverage import CoverageStatus
from core.pit_schema import PITStore
from core.source_registry import OPENDART_XBRL, default_registry
from feature_store.segment_exposure import Taxonomy, compute_exposure
from tests.fixtures.xbrl_sample import ENTITY, build

UTC = timezone.utc
CONS = ("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", "ifrs-full:ConsolidatedMember")
SEP = ("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", "ifrs-full:SeparateMember")
SEG, ITEMS, GEO = "ifrs-full:SegmentsAxis", "ifrs-full:SegmentConsolidationItemsAxis", "ifrs-full:GeographicalAreasAxis"
OPS = (ITEMS, "ifrs-full:OperatingSegmentsMember")
Y = ("2025-01-01", "2025-12-31")


def seg_member(name, table="DisclosureOfOperatingSegmentsTable"):
    return f"{ENTITY}:{name}MemberOfReportableSegmentsMemberOf{table}OfMember"


def samsung_like(ds_oi=24_900):
    """영업부문 표(DS·DX·SDC·Harman) + 같은 부문이 다른 표에도 나옴 + 합계 멤버 + 지역 두 묶음 + 별도 기준 값."""
    ctx, facts, labels = [("C0", *Y, (CONS,))], [("ifrs-full:Revenue", "C0", 333_000, "KRW")], {}
    segs = {"Dx": ("DX 부문 [구성요소]", 188_000, 12_900), "Ds": ("DS 부문 [구성요소]", 130_000, ds_oi),
            "Sdc": ("SDC [구성요소]", 30_000, 4_100), "Harman": ("Harman [구성요소]", 16_000, 1_500)}
    for i, (name, (label, rev, oi)) in enumerate(segs.items()):
        m = seg_member(name)
        labels[m] = label
        ctx.append((f"S{i}", *Y, (CONS, OPS, (SEG, m))))
        facts += [("ifrs-full:Revenue", f"S{i}", rev, "KRW"), ("dart:OperatingIncomeLoss", f"S{i}", oi, "KRW")]
    # 같은 'DS 부문'이 감가상각 표에도 있다 → 무시해야 함
    other = seg_member("Ds", "DisclosureOfDepreciationTable")
    labels[other] = "DS 부문 [구성요소]"
    ctx.append(("X1", *Y, (CONS, (SEG, other))))
    facts.append(("ifrs-full:Revenue", "X1", 999_999, "KRW"))
    # 공사계약 표 → 무시
    contract = seg_member("Dx", "ChangesInConstructionContractTable")
    ctx.append(("X2", *Y, (CONS, (SEG, contract))))
    facts += [("ifrs-full:Revenue", "X2", 1, "KRW"), ("dart:OperatingIncomeLoss", "X2", 1, "KRW")]
    # 합계 멤버 → 부문 아님
    ctx.append(("T", *Y, (CONS, OPS, (SEG, "ifrs-full:OperatingSegmentsMember"))))
    facts.append(("ifrs-full:Revenue", "T", 364_000, "KRW"))
    # 별도 기준 부문 값 → 연결이 있으면 쓰지 않음
    ctx.append(("SP", *Y, (SEP, (SEG, seg_member("Ds")))))
    facts.append(("ifrs-full:Revenue", "SP", 5, "KRW"))
    # 지역: IFRS 8 (Revenue) 묶음과 다른 개념 묶음
    for i, (m, label, v) in enumerate([("ifrs-full:CountryOfDomicileMember", "본사 소재지 국가 [구성요소]", 46_000),
                                       ("dart:USMember", "미국 [구성요소]", 133_000),
                                       ("dart:CNMember", "중국 [구성요소]", 47_000),
                                       (f"{ENTITY}:EuropeMemberOfForeignCountriesMemberOfDisclosureOfGeographicalAreasTableOfMember",
                                        "유럽 [구성요소]", 53_000)]):
        labels[m] = label
        ctx.append((f"G{i}", *Y, (CONS, (GEO, m))))
        facts.append(("ifrs-full:Revenue", f"G{i}", v, "KRW"))
        ctx.append((f"H{i}", *Y, (CONS, (GEO, m))))
    for i, m in enumerate(["dart:USMember", "dart:CNMember"]):
        ctx.append((f"R{i}", *Y, (CONS, (GEO, m))))
        facts.append(("ifrs-full:RevenueFromContractsWithCustomers", f"R{i}", 1_000, "KRW"))
    # 이전 기간 값 → 당기만 써야 함
    ctx.append(("P", "2024-01-01", "2024-12-31", (CONS, OPS, (SEG, seg_member("Ds")))))
    facts.append(("ifrs-full:Revenue", "P", 77, "KRW"))
    labels["ifrs-full:CountryOfDomicileMember"] = "본사 소재지 국가 [구성요소]"
    return build(contexts=ctx, facts=facts, labels=labels)


def posco_like():
    """영업이익 없이 부문 당기순이익만 공시."""
    ctx, facts, labels = [], [], {}
    for i, (name, label, rev, pl) in enumerate([("Steel", "철강", 59_000, 2_000), ("Trading", "무역", 42_000, -300),
                                                ("Battery", "이차전지소재", 3_300, -500)]):
        m = f"{ENTITY}:{name}MemberOfSegmentsMemberOfDisclosureOfOperatingSegmentsTableOfMember"
        labels[m] = label
        ctx.append((f"S{i}", *Y, (CONS, OPS, (SEG, m))))
        facts += [("ifrs-full:Revenue", f"S{i}", rev, "KRW"), ("ifrs-full:ProfitLoss", f"S{i}", pl, "KRW")]
    return build(contexts=ctx, facts=facts, labels=labels)


def single_segment():
    return build(contexts=[("C0", *Y, (CONS,))], facts=[("ifrs-full:Revenue", "C0", 100, "KRW")])


def test_member_table_parsing():
    assert member_table(seg_member("Ds")) == "DisclosureOfOperatingSegmentsTable"
    assert member_table("ifrs-full:AllOtherSegmentsMember") is None


def test_extract_picks_operating_segment_table_only():
    ext = extract_segments(parse_xbrl_zip(samsung_like()))
    assert ext.segment_status == "OK" and ext.segment_table == "DisclosureOfOperatingSegmentsTable"
    seg = {(v.key, v.role): v.value for v in ext.values if v.kind == "segment"}
    assert seg == {("DX 부문", "revenue"): 188_000, ("DX 부문", "operating_income"): 12_900,
                   ("DS 부문", "revenue"): 130_000, ("DS 부문", "operating_income"): 24_900,
                   ("SDC", "revenue"): 30_000, ("SDC", "operating_income"): 4_100,
                   ("Harman", "revenue"): 16_000, ("Harman", "operating_income"): 1_500}
    assert all(v.consolidation == "CONSOLIDATED" and v.end == date(2025, 12, 31) for v in ext.values)
    geo = {v.key: v.value for v in ext.values if v.kind == "geography"}
    assert geo == {"본사 소재지 국가": 46_000, "미국": 133_000, "중국": 47_000, "유럽": 53_000}
    assert ext.geography_group == "CONSOLIDATED|-|ifrs-full:Revenue"


def test_extract_profit_fallback_and_no_segments():
    ext = extract_segments(parse_xbrl_zip(posco_like()))
    assert ext.segment_status == "PROFIT_NOT_OPERATING"
    assert {v.role for v in ext.values} == {"revenue", "segment_profit"}
    none = extract_segments(parse_xbrl_zip(single_segment()))
    assert none.segment_status == "NO_SEGMENT_TABLE" and none.values == [] and none.period_end == date(2025, 12, 31)


def _store_with(payload, rcept_no="20260310002820", ingested=datetime(2026, 3, 10, 21, 0, tzinfo=UTC), store=None):
    store = store or PITStore(default_registry())
    ext = extract_segments(parse_xbrl_zip(payload))
    recs, same = segment_records(ext, corp_code="00126380", rcept_no=rcept_no, rcept_dt=date.fromisoformat(
        f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"), payload_sha256="h", contract_id=OPENDART_XBRL.contract_id,
        ingested_at_utc=ingested, latest=store.latest)
    for r in recs:
        store.append(r)
    return store, recs, same


def test_exposure_weights_and_confidence():
    store, recs, _ = _store_with(samsung_like())
    assert all(r.series_id == SERIES_SEGMENTS and r.license_contract_id == "opendart_xbrl_v1" for r in recs)
    assert json.loads(recs[0].value) == {"segment": "OK", "geography": "OK"}
    rep = compute_exposure(store, "00126380", datetime(2026, 3, 12, tzinfo=UTC), Taxonomy.load())
    assert rep.status == CoverageStatus.SUFFICIENT and rep.profit_weight_status == "OK"
    w = rep.industry_revenue_weights
    assert w["consumer_electronics_mobile"] == pytest.approx(188 / 364)
    assert w["semiconductor"] == pytest.approx(130 / 364)
    assert rep.industry_profit_weights["semiconductor"] == pytest.approx(24.9 / 43.4)    # 이익으로 보면 반도체가 더 크다
    assert sum(rep.region_weights.values()) == pytest.approx(1.0) and rep.region_weights["US_NA"] == pytest.approx(133 / 279)
    assert {s.key: s.confidence for s in rep.segments}["DS 부문"] == "MEDIUM"
    assert all(s.driver_coverage == "NOT_COLLECTED" for s in rep.segments)


def test_exposure_is_point_in_time():
    store, _, _ = _store_with(samsung_like())
    before = compute_exposure(store, "00126380", datetime(2026, 3, 10, 23, 29, tzinfo=UTC))   # 3/11 08:30 KST 직전
    assert before.status == CoverageStatus.INSUFFICIENT and before.segments == ()
    after = compute_exposure(store, "00126380", datetime(2026, 3, 10, 23, 30, tzinfo=UTC))
    assert len(after.segments) == 4
    stale = compute_exposure(store, "00126380", datetime(2027, 12, 1, tzinfo=UTC))
    assert stale.status == CoverageStatus.STALE


def test_negative_profit_limits_profit_weights_and_no_segment_is_insufficient():
    store, _, _ = _store_with(posco_like())
    rep = compute_exposure(store, "00126380", datetime(2026, 3, 12, tzinfo=UTC))
    assert rep.profit_role == "segment_profit" and rep.profit_weight_status == "LIMITED"
    assert rep.status == CoverageStatus.LIMITED and any("음수" in r for r in rep.reasons)
    store2, _, _ = _store_with(single_segment())
    rep2 = compute_exposure(store2, "00126380", datetime(2026, 3, 12, tzinfo=UTC))
    assert rep2.status == CoverageStatus.INSUFFICIENT and rep2.segments == () and "추정하지 않음" in rep2.reasons[0]


def test_correction_creates_revision_only_for_changed_values():
    store, recs, _ = _store_with(samsung_like())
    payload = samsung_like(ds_oi=20_000)                               # 정정: DS 영업이익만 바뀜
    store, recs2, same = _store_with(payload, rcept_no="20260320000001", ingested=datetime(2026, 3, 20, 21, tzinfo=UTC),
                                     store=store)
    changed = [r for r in recs2 if r.metric_id != "extraction_status"]
    assert [r.metric_id for r in changed] == ["CONSOLIDATED|segment|DS 부문|operating_income"]
    assert changed[0].supersedes_revision_id.startswith("20260310002820|") and same >= 10
    rep = compute_exposure(store, "00126380", datetime(2026, 3, 25, tzinfo=UTC))
    assert {s.key: s.profit for s in rep.segments}["DS 부문"] == 20_000
