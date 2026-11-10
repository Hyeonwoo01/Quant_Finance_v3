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

# 고유번호 파일: 회사 식별 정보(고유번호·회사명·종목코드)뿐이라 공시 목록과 같은 조건. 원본 zip은 용량 때문에 저장하지 않는다
# (계약상 금지가 아니라 운영상 선택 — collectors/opendart_corpcode.py).
OPENDART_CORPCODE = DataSourceContract(
    contract_id="opendart_corpcode_v1", provider="OpenDART", source_id="opendart_corpcode",
    series_or_endpoint="corpCode.xml (고유번호: 회사별 고유번호·회사명·종목코드·최종변경일)", frequency="daily",
    unit="mapping", availability_policy_id="ingested_next_kr_cutoff",
    revision_policy="회사명·종목코드가 바뀌면 새 revision, 종목코드가 사라지면 빈 종목코드의 새 revision",
    max_staleness_policy_id="kr_daily_1_session", **_OPENDART_PERMISSIONS)

# XBRL 원본 zip에는 서술형 주석(공시 제출인의 저작물일 수 있음)이 들어 있다. 원본은 저장·재배포·LLM 입력하지 않고,
# 메모리에서 숫자 사실(부문별·지역별 매출·영업이익 등)만 뽑아 저장한다. 숫자 사실은 전체 재무제표 API와 같은 성격이다.
OPENDART_XBRL = DataSourceContract(
    contract_id="opendart_xbrl_v1", provider="OpenDART", source_id="opendart_xbrl",
    series_or_endpoint="fnlttXbrl.xml (재무제표 원본파일 XBRL: 주석의 부문·지역 숫자 사실만 추출)", frequency="quarterly",
    unit="currency_amount", availability_policy_id="dart_next_krx_session_0830_or_ingested",
    revision_policy="[기재정정] 보고서의 값이 바뀐 사실만 새 revision", max_staleness_policy_id="kr_quarterly_report",
    **{**_OPENDART_PERMISSIONS, "archival_allowed": False, "llm_input_allowed": False,
       "raw_redistribution_allowed": False, "approval_status": ApprovalStatus.APPROVED_FOR_PUBLIC_DISPLAY})

# ---------------- 산업·거시 공공 데이터 (DATA_SOURCES.md, 2026-11-09 원문 확인) ----------------
_REVIEWED = datetime(2026, 11, 9, tzinfo=timezone.utc)
_OPEN = dict(storage_allowed=True, caching_allowed=True, archival_allowed=True, ml_training_allowed=True,
             llm_input_allowed=True, public_display_allowed=True, raw_redistribution_allowed=True,
             derived_data_use_allowed=True, approval_status=ApprovalStatus.APPROVED_FOR_REDISTRIBUTION,
             license_reviewed_at=_REVIEWED, attribution_required=True, fallback_source_id=None)

# 관세청 품목별 수출입실적: 데이터셋 '이용허락범위 제한 없음'. API가 최신 값만 주므로 가용 시각은 실제 수집 시각.
KCS_ITEMTRADE = DataSourceContract(
    contract_id="kcs_itemtrade_v1", provider="KCS", source_id="kcs_itemtrade",
    series_or_endpoint="apis.data.go.kr/1220000/Itemtrade/getItemtradeList (HS 품목별 월간 수출입)",
    frequency="monthly", unit="USD|KG", currency="USD", native_timezone="Asia/Seoul",
    availability_policy_id="ingested_only_revised_only_history",
    revision_policy="API는 최신 값만 제공 → 값이 바뀌면 새 revision, 수집 전 과거 값은 REVISED_ONLY",
    license_url="https://www.data.go.kr/data/15101609/openapi.do",
    permission_reference="공공데이터포털 데이터셋 15101609 '이용허락범위 제한 없음' (2026-11-09 확인)",
    max_staleness_policy_id="monthly_release", **_OPEN)

# 한국은행 ECOS: 상업적 이용 가능(제5조), 출처 표시 의무(제7조②). 다른 기관이 작성한 통계는 시리즈별 확인 전 사용 안 함.
BOK_ECOS = DataSourceContract(
    contract_id="bok_ecos_v1", provider="BOK", source_id="bok_ecos",
    series_or_endpoint="ecos.bok.or.kr Open API (한국은행 작성 통계에 한함)", frequency="mixed", unit="mixed",
    currency=None, native_timezone="Asia/Seoul", availability_policy_id="ingested_only_revised_only_history",
    revision_policy="API는 최신 값만 제공 → 값이 바뀌면 새 revision, 수집 전 과거 값은 REVISED_ONLY",
    license_url="https://ecos.bok.or.kr/api/",
    permission_reference="한국은행 경제통계 Open API 이용약관(2020-11-05 시행) 제5조·제7조, 2026-11-09 원문 확인",
    max_staleness_policy_id="series_release", **_OPEN)

# 미국 EIA: 미국 정부 저작물(퍼블릭 도메인). 민간 자료를 재게시하는 시리즈는 출처 확인 전 사용 안 함.
EIA = DataSourceContract(
    contract_id="eia_v1", provider="EIA", source_id="eia",
    series_or_endpoint="api.eia.gov v2 (EIA가 직접 조사·작성한 시리즈에 한함)", frequency="mixed", unit="mixed",
    currency="USD", native_timezone="America/New_York", availability_policy_id="release_schedule_or_ingested",
    revision_policy="값이 바뀌면 새 revision", license_url="https://www.eia.gov/about/copyrights_reuse.php",
    permission_reference="EIA Copyrights and Reuse + API Terms of Service, 2026-11-09 원문 확인",
    max_staleness_policy_id="series_release", **_OPEN)

# 금융위원회 주식시세정보 (원천: 한국거래소). 공공누리 제4유형 + "상업적 목적 여부와 상관 없이 제3자 무단 제공 및
# 재배포가 엄격히 금지". 그래서 비공개 저장소에만 두고(storage/visibility.py), 공개 표시·원본 재배포는 막는다.
# ML 학습·파생 지표는 '변경금지'가 미치는지 원문에 없어 한국거래소 답변을 받기 전까지 막는다 (Fail Closed).
# 답변을 받으면 이 계약을 고치지 않고 새 contract_id(v2)로 등록한다.
FSC_KRX_STOCK_PRICE = DataSourceContract(
    contract_id="fsc_krx_stock_price_v1", provider="FSC", source_id="fsc_krx_stock_price",
    series_or_endpoint="apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2 (일별 주식 시세)",
    frequency="daily", unit="KRW|shares", currency="KRW", native_timezone="Asia/Seoul",
    availability_policy_id="provider_next_business_day_1300_then_kr_cutoff",
    revision_policy="같은 종목·기준일의 값이 바뀌면 새 revision",
    storage_allowed=True, caching_allowed=True, archival_allowed=True, ml_training_allowed=False,
    llm_input_allowed=False, public_display_allowed=False, raw_redistribution_allowed=False,
    derived_data_use_allowed=False, approval_status=ApprovalStatus.APPROVED_FOR_INTERNAL_USE,
    license_url="https://www.data.go.kr/data/15094808/openapi.do", license_reviewed_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
    permission_reference="공공데이터포털 데이터셋 15094808 설명 (공공누리 제4유형, 제3자 제공·재배포 금지), 2026-11-10 원문 확인 — "
                         "DATA_SOURCES.md '한국 주식 가격'. 비공개 저장·개인 비상업 분석만",
    attribution_required=True, fallback_source_id=None, max_staleness_policy_id="kr_daily_2_sessions")

REGISTERED_CONTRACTS = (OPENDART_LIST, OPENDART_FINANCIALS, OPENDART_XBRL, OPENDART_CORPCODE, KCS_ITEMTRADE, BOK_ECOS, EIA,
                        FSC_KRX_STOCK_PRICE)


def default_registry() -> ContractRegistry:
    return ContractRegistry(REGISTERED_CONTRACTS)
