"""백테스트용 가용 시각 (가이드 0-2의 `backtest_available_at`, 4-1·4-4).

`available_at_utc`는 "이 시스템이 그 값을 실제로 쓸 수 있게 된 시각"이다. 받은 뒤의 첫 08:30 컷오프보다 이를 수
없으므로, 2026년에 과거 채우기로 받은 2010년 시세는 2026년부터 쓸 수 있는 값으로 기록된다. 이것은 실전 기록으로는
맞지만, 그대로 백테스트에 쓰면 과거 시점에서 시세가 하나도 보이지 않는다.

백테스트에서는 그 값이 **당시 공급자에게서 이용 가능했던 시각**을 쓴다. 다만 이 규칙은 아래 조건을 모두 만족하는
값에만 쓴다. 나머지는 `available_at_utc` 그대로다.

1. 공개 뒤 정정되지 않는 시장 확정값 시리즈 (IMMUTABLE_MARKET_SERIES: 거래소 일별 시세·기준가)
2. 원본 레코드 (정정본이 아님: supersedes_revision_id 없음). 정정본은 실제로 받은 뒤부터만 쓴다
3. 공급자 가용 시각(provider_available_at_utc)이 기록돼 있음. 그 시각 뒤 첫 08:30 KST 컷오프부터 쓴다

재무제표·공시·거시 지표처럼 정정·개정되는 값에는 원칙적으로 쓰지 않는다. 과거 채우기로 받은 시세는 받을 때의
공급자 값이고, 그 사이에 공급자가 고쳤는지는 알 수 없다. 거래소 확정 시세는 그럴 일이 드물어 받아들인 가정이다
(MASTER_GUIDE 4-4 구현 노트).

예외 BACKFILL_PROVIDER_TIME_SERIES: 과거 재무(dart_fin_hist)는 공급자 시각을 그 보고서의 **마지막 재무 변경일**
(최초 제출·기재정정 중 가장 늦은 것)로 정해 두었다. 그래서 저장된 최신 값은 그 시각부터 실제로 공개돼 있던 값이다
(정정 전 기간은 비워 둔다). 정기보고서 목록(dart_periodic_index)은 접수 사실이라 바뀌지 않는다.
"""
from datetime import datetime
from typing import Optional

from core.calendar_align import first_kr_cutoff_at_or_after
from core.pit_schema import PITRecord

IMMUTABLE_MARKET_SERIES = frozenset({"krx_openapi_daily", "krx_openapi_base", "krx_daily_price"})
# 과거 채우기로 받았지만 공급자 시각을 보수적으로 정한 시리즈 (collectors/opendart_history.py):
# 정기보고서 목록(접수 사실은 바뀌지 않음)과, 마지막 재무 변경일 기준으로 가용 시각을 둔 과거 재무 값.
# ECOS 금리 과거 채우기(collectors/ecos_history.py)도 발표 시각 규칙으로 공급자 시각을 정했다 (금리는 정정·재기준 없음).
BACKFILL_PROVIDER_TIME_SERIES = frozenset({"dart_periodic_index", "dart_fin_hist", "bok_ecos_hist"})
PROVIDER_TIME_SERIES = IMMUTABLE_MARKET_SERIES | BACKFILL_PROVIDER_TIME_SERIES


def uses_provider_time(series_id: str, supersedes_revision_id: Optional[str],
                       provider_available_at_utc: Optional[datetime]) -> bool:
    return (series_id in PROVIDER_TIME_SERIES and not supersedes_revision_id
            and provider_available_at_utc is not None)


def backtest_available_at(rec: PITRecord) -> datetime:
    if uses_provider_time(rec.series_id, rec.supersedes_revision_id, rec.provider_available_at_utc):
        return first_kr_cutoff_at_or_after(rec.provider_available_at_utc).to_pydatetime()
    return rec.available_at_utc
