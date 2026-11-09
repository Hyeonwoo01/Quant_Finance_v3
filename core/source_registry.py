"""등록된 데이터 공급원 계약 (가이드 3-1, 17). 근거와 판단은 DATA_SOURCES.md에 적는다.

새 공급원은 약관을 원문으로 확인한 뒤 여기에 계약을 추가하고, 그 다음에 수집기를 만든다. 계약을 바꿀 때는 기존 계약을
고치지 않고 새 contract_id로 등록한다 (레지스트리가 같은 id의 재등록을 막는다).
"""
from datetime import datetime, timezone

from core.data_contracts import ApprovalStatus, ContractRegistry, DataSourceContract

OPENDART_TERMS_URL = "https://opendart.fss.or.kr/intro/terms.do"
_OPENDART_REVIEWED = datetime(2026, 11, 9, tzinfo=timezone.utc)
_OPENDART_REFERENCE = "OpenDART 이용약관(2020-01-21 시행) 2026-11-09 원문 검토 — DATA_SOURCES.md#opendart"

# OpenDART: 약관에 저장·가공·재배포·상업적 이용 제한 조항이 없고, 약관에 없는 사항은 공공데이터법·저작권법을 따른다
# (약관 제5조, 제16조 ④). 공시 원문 문서(본문·첨부)는 받지 않는다 — 작성자(공시 제출인)의 저작물일 수 있어 따로 검토한다.
_OPENDART_PERMISSIONS = dict(
    storage_allowed=True, caching_allowed=True, archival_allowed=True, ml_training_allowed=True,
    llm_input_allowed=True, public_display_allowed=True, raw_redistribution_allowed=True, derived_data_use_allowed=True,
    approval_status=ApprovalStatus.APPROVED_FOR_REDISTRIBUTION, license_url=OPENDART_TERMS_URL,
    license_reviewed_at=_OPENDART_REVIEWED, permission_reference=_OPENDART_REFERENCE,
    attribution_required=True,          # 출처: 금융감독원 전자공시시스템(DART)
    fallback_source_id=None, native_timezone="Asia/Seoul", currency=None,
)

OPENDART_LIST = DataSourceContract(
    contract_id="opendart_list_v1", provider="OpenDART", source_id="opendart_list",
    series_or_endpoint="list.json (공시검색: 접수번호·접수일·보고서명·제출인·비고)", frequency="event",
    unit="filing", availability_policy_id="dart_next_krx_session_0830_or_ingested",
    revision_policy="같은 접수번호의 비고(rm) 변경은 새 revision, 정정공시는 별도 접수번호",
    max_staleness_policy_id="kr_daily_1_session", **_OPENDART_PERMISSIONS)

OPENDART_FINANCIALS = DataSourceContract(
    contract_id="opendart_fnltt_v1", provider="OpenDART", source_id="opendart_fnltt",
    series_or_endpoint="fnlttSinglAcntAll.json (단일회사 전체 재무제표: BS·IS·CIS·CF)", frequency="quarterly",
    unit="currency_amount", availability_policy_id="dart_next_krx_session_0830_or_ingested",
    revision_policy="[기재정정] 보고서는 바뀐 계정만 새 revision으로 이전 버전을 가리킴",
    max_staleness_policy_id="kr_quarterly_report", **_OPENDART_PERMISSIONS)

# XBRL 원본 zip에는 서술형 주석(공시 제출인의 저작물일 수 있음)이 들어 있다. 원본은 저장·재배포·LLM 입력하지 않고,
# 메모리에서 숫자 사실(부문별·지역별 매출·영업이익 등)만 뽑아 저장한다. 숫자 사실은 전체 재무제표 API와 같은 성격이다.
OPENDART_XBRL = DataSourceContract(
    contract_id="opendart_xbrl_v1", provider="OpenDART", source_id="opendart_xbrl",
    series_or_endpoint="fnlttXbrl.xml (재무제표 원본파일 XBRL: 주석의 부문·지역 숫자 사실만 추출)", frequency="quarterly",
    unit="currency_amount", availability_policy_id="dart_next_krx_session_0830_or_ingested",
    revision_policy="[기재정정] 보고서의 값이 바뀐 사실만 새 revision", max_staleness_policy_id="kr_quarterly_report",
    **{**_OPENDART_PERMISSIONS, "archival_allowed": False, "llm_input_allowed": False,
       "raw_redistribution_allowed": False, "approval_status": ApprovalStatus.APPROVED_FOR_PUBLIC_DISPLAY})

REGISTERED_CONTRACTS = (OPENDART_LIST, OPENDART_FINANCIALS, OPENDART_XBRL)


def default_registry() -> ContractRegistry:
    return ContractRegistry(REGISTERED_CONTRACTS)
