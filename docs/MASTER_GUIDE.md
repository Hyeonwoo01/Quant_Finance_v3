# Quant_Finance_v3 마스터 개발 가이드 v2.2

## Global Quant & AI Investment Intelligence Platform

**문서 상태:** 설계 기준 문서(Engineering Specification Draft)  
**구현 저장소:** [Hyeonwoo01/Quant_Finance_v3](https://github.com/Hyeonwoo01/Quant_Finance_v3)  
**이전 저장소:** [Hyeonwoo01/Quant_Finance_Project_KOR](https://github.com/Hyeonwoo01/Quant_Finance_Project_KOR) (한국 전용 v2), [Hyeonwoo01/Quant_Finance_Project](https://github.com/Hyeonwoo01/Quant_Finance_Project) (미국·글로벌 v1)  
**기준일:** 2026-11-09 (v2.1: 2026-11-06)  
**대상 시장:** 한국 주식시장 우선, 미국·글로벌 시장 데이터 연계  
**핵심 기술 후보:** Python, pandas, NumPy, DuckDB, Parquet, PostgreSQL(확장 시), exchange_calendars, scikit-learn, LightGBM, arch, FastAPI(확장 단계), Streamlit, Pydantic, LLM Tool Calling, Hybrid RAG

> **중요한 상태 고지**  
> 이 문서는 제품 및 연구 시스템의 목표 설계와 검증 기준을 정의한다. 문서에 기재된 기능·테스트 수·데이터 권한·성능은 실제 구현 또는 통과가 확인되기 전까지 완료된 것으로 간주하지 않는다. 이전 저장소의 기존 자산(예: Quant_Finance_Project_KOR의 86개 테스트)은 기준 스냅샷으로만 취급하며, 옮겨 쓸 때는 이 저장소에서 다시 실행해 통과 여부를 확인한다.

---

# 0. 변경 요약 및 개발 원칙

## 0-1. v2.1에서 v2.2로 변경한 핵심 사항

외부 검토안(Gemini가 작성한 v2.1 변형본)을 대조해, 기능을 늘리는 아이디어가 아니라 **분석 깊이와 운영 신뢰성을 높이는 설계 아이디어 8개**만 가져왔다.
v2.1에 이미 있던 설계(5축 시장 국면, Purged Walk-Forward·Embargo와 모델 비교 검정, FCFF/FCFE와 업종별 밸류에이션,
Financial Health·Investment Attractiveness·Market Risk 분리, 기간별 시그널, Data Integrity Circuit Breaker와 Market Risk Control 분리,
PIT 기업행위 조정, Forecast Audit Trail, Evidence-Grounded AI)는 중복해서 추가하지 않고 세부만 보완했다.
그대로 가져오지 않은 제안과 이유는 [부록 A](#부록-a-외부-검토안-중-채택하지-않은-제안)에 둔다.

| 우선순위 | 아이디어 | 반영 내용 | 위치 |
|---|---|---|---|
| 1 | 사업부문별 글로벌 산업 노출도 | 기업 → 사업부문 → 글로벌 산업지표의 2단계 구조. 매출·영업이익 비중, 지역 노출, 생산능력·가동률, 수주잔고·출하량을 공시 시점 기준(PIT)으로 보관하고, 기여 여부는 Model A/B/C로 검증 | 5-2, 5-3 |
| 2 | 데이터 커버리지에 따른 분석 가능 여부 | 모든 종목에 같은 최소 이력(예: 252거래일·8분기)을 강제하지 않고 분석 엔진마다 필요 데이터를 정의. 상태는 `SUFFICIENT` / `LIMITED` / `INSUFFICIENT` / `STALE` | 3-6, 9, 10, 11 |
| 3 | Strategy Lab 실험 기록과 Final OOS Vault | 사용자 실험도 과적합을 만든다. 탐색·검증·잠긴 최종 OOS 구간을 나누고 모든 실험을 자동 기록해 다중검정·선택 편향 평가에 사용 | 7-9, 7-11, 7-12, 13 |
| 4 | 거래 가능성을 반영한 Forecast Audit | 데이터 컷오프·진입 가정·진입 가능 여부·체결 가격·비용을 연결하고, 예측 정확도와 투자 성과를 분리. 실제 체결과 시뮬레이션 체결 구분 | 7-10, 8-5 |
| 5 | 다중 시간 창 Drift Monitoring | 5개 Drift 유형 × 20·60·120거래일 창, 경보 뒤의 행동 단계(정상·Warning·Critical·재검증·승인). 임계값은 실제 데이터로 정함 | 15-4 |
| 6 | Fair Value 민감도 UI | 할인율·성장률 변화에 따른 가치 변화표와, 업종별 적용 모델·제외 이유를 사용자에게 표시 | 10-5 |
| 7 | AI 근거 추적 강화 | `EvidenceClaim`에 기준 기간 유형·연결/별도·계산 방식·도구 호출 ID·원문 위치·검증 상태를 추가하고, 숫자 일치가 아니라 의미 일치(영업이익 ↔ 순이익, 분기 ↔ 연간)까지 검증 | 14-2 |
| 8 | 사업부문별 분석 신뢰도 표시 | 기업 전체 노출도를 한 점수로 뭉개지 않고, 사업부문·드라이버별 커버리지와 신뢰도를 따로 표시 | 5-3, 11-3 |

### v2.2 정정 (2026-11-09, v2.1을 만든 검토 대화 재확인)

v2.1을 만든 검토 대화에서 v2.1 스스로의 오류로 지적됐거나 제미나이 변형본의 오류로 지적됐지만 v2.2에 아직 남아 있던 부분을 고쳤다.
앞의 두 항목은 구현 코드(`core/calendar_align.py`)에도 같은 문제가 있어 함께 고쳤다.

| 항목 | 문제 | 정정 | 위치 |
|---|---|---|---|
| 그룹별 as-of 정렬 | 예시 코드가 `series_id`만으로 묶어, 같은 지표 이름(예: `revenue`)을 쓰는 여러 회사의 값이 섞일 수 있음 | 그룹 키를 `entity_id + series_id`로 고정 | 4-4 |
| 휴장 중 미국 누적수익률 | 빠진 미국 세션과 미국 휴장을 구분하지 않아, 결측이 큰 움직임을 숨기거나 0으로 처리될 수 있음 | 거래소 캘린더의 예정 세션과 받은 종가를 대조. 빠지면 계산하지 않고 `INCOMPLETE` | 4-5 |
| Staleness 계약 조회 | 시리즈 ID로 계약을 추측하면 못 찾고 기본값(예: 10일)이 적용될 수 있음 | 레코드의 `license_contract_id`로 찾고, 못 찾으면 막음 | 4-7 |
| 가격제한폭 계산 | 상·하한가 각각의 가격대 호가단위를 쓰는 구현과 기준가격 호가단위를 쓰는 규정의 차이, 호가단위 개편 전후 차이 | 규정 원문 확인 후 구현·테스트 고정, 날짜별 호가단위 표, '0% 체결'은 가정으로 표시 | 8-2 |
| Risk Parity | 위험기여도 상한 관리를 Risk Parity로 부를 수 있고, 결과 검증 규칙이 없음 | 둘을 구분하고 수렴·공분산·순서·합계 검증 추가 | 9-5 |
| 업종별 밸류에이션 | 지주회사와 경기순환 업종 행이 없음. 민감도 범위·점수 가중치를 고정값으로 쓸 위험 | SOTP·정상화 이익 행 추가, 범위·가중치의 근거 기록 | 10-2, 10-5 |

같은 대화의 앞쪽 검토(v1·v2.0 검토, 제품 방향 논의)를 항목별로 v2.2와 대조해 빠진 기술·데이터 항목도 보완했다.

| 항목 | 보완 | 위치 |
|---|---|---|
| 거시 빈티지 출처 | 원천기관 직접 수집만으로 과거 초판 값이 확보되지 않음. 초판 출처를 계약으로 등록하거나 `REVISED_ONLY`로 표시해 기본 피처에서 제외 | 3-2, 7-2 |
| 컨센서스·추정치 | 공급원·권한·스냅샷 규칙 행 추가 | 3-3 |
| DEXKOUS 공개 지연 | 원천 H.10은 주 1회(지난주 일별 값을 월요일) 공개 | 3-4 |
| 계약 상태 | 단일 상태는 검토 단계의 요약이고, 허용 여부는 목적별 권한 필드로만 판정 | 3-5, 18 |
| 잠정실적 | 잠정(공정공시) → 확정(정기보고서)을 같은 관측값의 버전으로 저장 | 4-3 |
| 대체거래소 | 넥스트레이드(2025-03-04 출범) 프리·애프터마켓, 08:30 컷오프와 진입 가격의 venue 명시, KRX 지연 개장일 | 4-4, 4-6, 7-3 |
| 종목 식별자 매핑 | 종목코드·ISIN·DART 고유번호·해외 티커를 시점별로 연결 | 4-8 |
| 검증 | 국면별·기간별 성과 분해, PBO/CSCV, 요인 조정 알파, 실패 원인 분석, 타깃 Drift | 7-8, 7-10, 15-4 |
| 밸류에이션·건전성 | 영구성장률과 재투자율의 일관성, 시나리오 확률의 근거, 금융 업권별 지표 | 10-2, 10-3, 10-5, 11-2 |
| 표시·운영 | 점수 ≠ 확률 표시, 반대 근거, 포트폴리오 성과 지표, 비용 모형 추정 데이터의 PIT, CI/CD·롤백, 추가 테스트 | 8-3, 11-3, 12-3, 13-1, 14-2, 15-5, 19-2 |

### 제품 범위 결정 (2026-11-09)

| 항목 | 결정 | 이유 | 위치 |
|---|---|---|---|
| 뉴스·이벤트 분석 | **DART 공시 이벤트 분석만 포함**, 뉴스 원문 분석은 제외 | 공시 목록은 이용 조건을 확인하고 매일 수집 중이다. 뉴스 원문은 별도 라이선스가 필요하다 | 5-6, 부록 A |
| 추천 포트폴리오 (ProPicks 방식) | **"전략 모의 포트폴리오 기록"으로만 포함**. 공개 추천 종목은 제외 | 불특정 다수 대상, 특히 유료 투자 조언은 유사투자자문업 신고 대상이 될 수 있다. 사전 등록 전략의 앞으로의 성과 기록은 검증 목적에 맞다 | 7-13, 부록 A |
| 투자자 유형별 화면, 사용자 알림·리포트 | **확장 단계로 미룸** | MVP의 검증 가치를 높이지 않고 범위만 키운다 | 18 확장 단계 |
| 이전 미국 저장소 자산 | **요인은 한국판을 직접 구축**, 옵션 내재변동성·VXN은 범위 밖 | 요인 조정 알파(7-8)에 요인이 필요하지만 공개 요인 라이브러리에 한국 단독 요인이 없다. 옵션·VXN은 v1에서 비공식 경로로 받던 데이터라 권한 확인 전에는 쓰지 않고, 한국 주식 분석의 핵심도 아니다 | 7-8, 부록 A |

## 0-2. v2.0에서 v2.1로 변경한 핵심 사항

| 영역 | v2.0의 남은 문제 | v2.1 수정 사항 |
|---|---|---|
| 데이터 권한 | `commercial_redistributable` 같은 단일 Boolean으로 권한을 표현 | 표시·저장·캐싱·ML 학습·LLM 입력·재배포 권한을 분리하고, 미검토 데이터는 기본 차단 |
| FRED/ALFRED | API 데이터의 저장·아카이빙·ML 학습 제한을 아키텍처에 반영하지 않음 | 현재 공개 약관 기준 FRED API/콘텐츠를 ML 학습·영구 저장 소스로 사용하지 않음. 원천기관 직접 수집 또는 별도 서면 허가된 공급원 사용 |
| PIT 스키마 | 빈티지 날짜와 실제 발표 시각을 혼동할 여지 | `observation_period`, `vintage_date`, `official_release_at`, `provider_available_at`, `system_ingested_at`, `backtest_available_at` 분리 |
| 한국·미국 시간 정렬 | 조기 폐장일 수동 입력, 단일 `merge_asof`, 모든 데이터의 동일한 시간 가정 | 거래소 캘린더별 세션 스케줄, 시리즈별 그룹 정렬, 데이터 공급 지연 반영, 휴장 중 누적수익률 별도 계산 |
| DART | `rcept_dt + 1일`을 달력상 다음 날로 처리할 가능성 | 정확한 공개 시각이 없으면 접수일 이후 **다음 KRX 거래 세션** 08:30 KST를 보수적 가용 시점으로 설정 |
| FX·선물 | 모든 환율·선물을 08:30 KST 스냅샷으로 간주 | 실제 공급자의 관측시각·세션·마감 규칙에 따라 관리. `DEXKOUS`와 실시간 USD/KRW 스냅샷을 구분 |
| FRED 시리즈 정의 | DTWEXBGS를 DXY로, FEDFUNDS를 FOMC 목표금리로 오해할 수 있음 | 광의 달러지수·ICE DXY·유효 연방기금금리·목표금리 범위를 구분 |
| 가격제한폭·체결 | `±29.5%` 일괄 미체결 규칙 및 단순 ±30% 가정 | 종목·상품·시장 규칙, 기준가격, 호가단위, 실제 상하한가를 계산. 일별 OHLC만으로 체결 큐를 알 수 없다는 한계 명시 |
| 슬리피지 | 계수 `eta`의 단위·추정 근거 불명확 | 단위, 추정 데이터, 주문 참여율, 상한, 보수적 시나리오 명시 |
| 리스크 사이징 | Volatility Targeting과 Risk Parity를 같은 것으로 취급 | 변동성 타겟팅·ES 예산·공분산 기반 위험기여도 최적화를 별도 기능으로 분리 |
| 재무 점수 | Valuation·Momentum·Market Risk를 Financial Health에 포함 | Corporate Financial Health, Investment Attractiveness, Market Risk의 3개 결과로 분리 |
| 모델 검증 | 다기간 라벨 중첩, 모델 비교 검정, 다중검정 통제 부족 | Purge/Embargo, DM·Clark-West(적용 조건에 한함), Block Bootstrap, Reality Check/SPA 등 사전 정의 |
| Fair Value | 밴드 산출 방법 및 FCFF/FCFE 할인율 정의 부족 | 현금흐름별 할인율, Equity Bridge, 기업 유형별 모델 적합성, Bear/Base/Bull 시나리오 명시 |
| Regime | 상호 배타적이지 않은 5개 국면을 단일 라벨로 분류 | 시장위험·통화정책·물가·산업 사이클·환율을 독립 축으로 추정 |
| AI | 숫자가 Tool JSON에 존재하는지만 확인 | 수치·단위·통화·지표명·기준일·출처·산식 의미까지 구조적으로 대조 |
| 규제 | 추천 용어 변경과 면책문구로 안전성을 확보할 수 있다는 오해 | 서비스 제공 방식·유료 여부·개인화·양방향 상담을 별도 법률 검토. 문구 변경은 규제 회피 수단이 아님 |
| 구조 | tests, api, storage, orchestration의 누락 | 디렉터리 구조와 Definition of Done에 명시 |

## 0-3. 절대 개발 원칙

1. **Data Permission Before Ingestion:** 수집하기 전에 데이터의 저장·학습·표시·재배포 권한을 검토한다.
2. **Availability Before Observation Date:** 관측 기간이 아니라 실제 투자자가 알 수 있었던 시점을 기준으로 데이터를 사용한다.
3. **No Forced Model Winner:** Model C가 Model A보다 우수하다는 것을 전제로 하지 않는다. 추가 데이터가 성능을 개선하지 못하면 그 결과를 그대로 기록한다.
4. **One Calculation Source:** 백테스트·API·UI·AI가 동일한 코어 계산 모듈을 사용한다.
5. **Separate Research from Product Claims:** 연구용 지표와 사용자에게 제공하는 시그널의 의미를 구분한다.
6. **Fail Closed on Data Integrity:** 필수 데이터가 누락·오염·시간 역전되면 추정값을 정상값처럼 대체하지 않고 해당 분석을 보류한다.
7. **No Unverified Investment Claim:** 과거 백테스트는 미래 수익 보장이 아니며, 검증되지 않은 예측을 추천 신호로 표시하지 않는다.
8. **Reproducibility:** 동일한 코드·데이터 스냅샷·환경·파라미터로 동일 결과를 재현할 수 있어야 한다.

## 0-4. 프로젝트 목표와 비목표

### 목표

- 한국 주식 분석에 미국 주식·글로벌 산업·거시경제 데이터가 추가적인 예측 정보를 제공하는지 검증한다.
- Point-in-Time 데이터 기반의 가격·수급·재무·거시 피처 스토어를 구축한다.
- 변동성·꼬리위험·기업가치·기업 재무건전성을 서로 구분해 계산한다.
- 검증된 범위 안에서 종목 탐색, 정량 시그널, 포트폴리오 위험 분석을 제공한다.
- 수치와 문서 근거를 구조적으로 검증하는 Evidence-Grounded AI를 구현한다.

### 비목표(초기 MVP)

- InvestingPro의 데이터 커버리지·실시간성·유료 리서치 전체를 복제하지 않는다.
- 모든 한국·미국 상장 종목과 모든 산업 데이터를 한 번에 수집하지 않는다.
- 검증 전 모델의 실시간 매매 자동 집행을 구현하지 않는다.
- 개인별 재산상황을 반영한 투자자문 또는 일임 서비스를 규제 검토 없이 제공하지 않는다.
- 무료 데이터가 상업적 재배포·ML 학습에 자유롭게 사용 가능하다고 가정하지 않는다.

---

# 1. 제품 정의 및 기능 범위

## 1-1. 제품 포지셔닝

**Global Quant & AI Investment Intelligence**는 글로벌 금융시장과 거시경제·산업 정보를 이용해 한국 주식의 기업가치, 재무건전성, 시장위험, 정량 신호를 분석하고 그 근거와 한계를 설명하는 투자 분석 플랫폼이다.

제품의 차별화는 지표의 숫자나 AI 문구가 아니라 다음 세 가지에 둔다.

- 분석에 사용한 데이터의 기준일·출처·가용 시점이 추적 가능하다.
- 예측 및 전략 성능이 Out-of-Sample과 비용 차감 기준으로 검증된다.
- 모델이 불확실하거나 데이터가 부족하면 그 상태를 명시하고 분석을 보류한다.

분석은 다음 흐름을 하나로 잇는다. 각 단계의 결과는 다음 단계의 입력이며, 단계마다 데이터 커버리지와 불확실성을 함께 넘긴다.

```text
기업의 사업구조 → 글로벌 산업 환경 → 거시경제 → 주가·위험 예측 → 기업가치 평가 → 포트폴리오 위험 관리 → AI 근거 설명
```

## 1-2. 핵심 제품 모듈

| 모듈 | 사용자 가치 | 의존 코어 |
|---|---|---|
| Global Market Radar | 미국 및 글로벌 시장의 전일 변화와 한국 시장 관련 변수 확인 | Data Pipeline, Global Features |
| Stock Intelligence | 종목별 현재 데이터, 재무, 가치, 위험, 글로벌 노출도 통합 확인 | PIT Store, Valuation, Health, Risk |
| Fair Value Range | 여러 가치평가 방식과 가정별 평가 범위 확인 | Fair Value Engine |
| Corporate Financial Health | 기업의 재무 안정성·수익성·현금흐름 품질 평가 | Financial Health Engine |
| Market Risk | 변동성·VaR·ES·낙폭·시장 민감도 평가 | Quant Risk Engine |
| Quant Signal | 검증된 정량 조건과 상태 요약 | Signal Synthesizer |
| Screener / Factor Lab | 사용자 정의 조건과 연구용 전략 실험 | Screener, Evaluation |
| Portfolio Risk | 공분산·ES·위험기여도·통화/업종 집중도 분석 | Portfolio Risk Engine |
| Evidence-Grounded Assistant | 재무 데이터와 공시·뉴스 근거를 결합한 설명 | Structured Tools, RAG |
| Forecast Audit | 과거 예측 및 시그널의 사후 결과 추적 | Forecast Audit Trail |
| Disclosure Event Study | 유상증자·전환사채·자사주·수주·잠정실적 등 공시 이후의 초과수익 분석 | PIT 공시 이벤트, Evaluation |
| Strategy Paper Portfolio | 사전 등록한 전략의 모의 포트폴리오 편입·제외와 성과 기록 (추천 아님) | Forecast Audit Trail, Backtester |

## 1-3. 종목별 글로벌 노출도

고정된 업종-티커 매핑은 초기 후보로만 사용한다. 최종적으로는 기업의 사업부문·지역별 매출 노출도와 산업별 공급망 연결을 Point-in-Time으로 관리한다.

예를 들어 반도체 기업을 모두 SOX 하나로 설명하지 않는다. 메모리·파운드리·장비·팹리스 등 사업부문별로 관련 지표가 달라질 수 있다. 산업별 티커는 실제 경쟁사 또는 시장 프록시인지 메타데이터에 구분한다.

---

# 2. 전체 아키텍처

## 2-1. 논리적 데이터 흐름

```text
[Data Source Governance / Contracts]
        │  권한·주기·단위·시간·빈티지·가용시각·폴백 검토
        ▼
[Collectors / Raw Payload Intake]
        │  API 수집, 원본 해시, 응답 코드, 수집시간 기록
        ▼
[Raw Immutable Store]
        │  권한이 허용된 원본만 보존
        ▼
[Normalization & PIT Versioning]
        │  타임존·단위·기업행위·빈티지·정정 이력
        ▼
[PIT Feature Store]
        │  query_as_of(decision_time), source_id, feature_version
        ▼
[Core Engines]
  ├─ Global Features / Regime
  ├─ Prediction / Quant Risk
  ├─ Fair Value / Financial Health
  ├─ Signal Synthesis / Portfolio Sizing
  └─ Screener
        │
        ├──────────────► [Evaluation & Experiment Registry]
        │                  동일 코어 엔진, 별도 검증 하네스
        ▼
[Service Layer / API]
        ├─ Streamlit UI (MVP)
        ├─ FastAPI (확장 단계)
        └─ Evidence-Grounded Assistant (읽기 전용 도구)

[Monitoring & Controls]
  ├─ Data Quality / Freshness Monitor
  ├─ Model Drift Monitor
  ├─ Data Integrity Circuit Breaker
  ├─ Market Risk Control
  └─ Audit Logs / Alerts
```

## 2-2. 아키텍처 원칙

- UI 및 AI 계층은 외부 데이터 수집이나 임의 모델 재학습을 수행하지 않는다.
- 백테스트와 서비스는 같은 엔진을 호출하되, 실행 설정과 데이터 스냅샷을 명시적으로 분리한다.
- 모든 분석 요청은 `as_of_timestamp`, `universe_snapshot_id`, `feature_snapshot_id`, `model_version`을 포함한다.
- 외부 API 장애 시 마지막 데이터를 최신 데이터로 위장하지 않는다. 데이터 나이와 상태를 노출한다.
- 외부 문서·뉴스는 비신뢰 입력으로 취급한다. 문서 내용이 시스템 명령이나 도구 권한을 변경할 수 없다.
- 저장·표시·학습 권한이 승인되지 않은 데이터는 Feature Store·모델 학습·AI 컨텍스트로 전달하지 않는다.

---

# 3. 데이터 거버넌스 및 공급원 정책

## 3-1. Data Source Contract

모든 데이터 시리즈는 수집기 구현 전에 계약 객체에 등록한다. `commercial_redistributable` 같은 단일 값으로 권한을 표현하지 않는다.

필수 필드:

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class ApprovalStatus(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    RESEARCH_ONLY = "research_only"
    APPROVED_FOR_INTERNAL_USE = "approved_for_internal_use"
    APPROVED_FOR_TRAINING = "approved_for_training"
    APPROVED_FOR_PUBLIC_DISPLAY = "approved_for_public_display"
    APPROVED_FOR_REDISTRIBUTION = "approved_for_redistribution"
    BLOCKED = "blocked"

@dataclass(frozen=True)
class DataSourceContract:
    contract_id: str
    provider: str
    source_id: str
    series_or_endpoint: str
    frequency: str
    unit: str
    currency: Optional[str]
    native_timezone: str
    availability_policy_id: str
    revision_policy: str

    # 권한은 각각 독립적으로 확인한다.
    storage_allowed: bool
    caching_allowed: bool
    archival_allowed: bool
    ml_training_allowed: bool
    llm_input_allowed: bool
    public_display_allowed: bool
    raw_redistribution_allowed: bool
    derived_data_use_allowed: bool

    approval_status: ApprovalStatus
    license_url: str
    license_reviewed_at: Optional[datetime]
    permission_reference: Optional[str]
    attribution_required: bool
    fallback_source_id: Optional[str]
    max_staleness_policy_id: str
```

**기본 차단 규칙:** 권한을 확인하지 못한 데이터는 `NOT_REVIEWED`로 등록하고, 외부 수집·영구 저장·ML 학습·LLM 입력·공개 화면 제공을 차단한다. 권한을 추정하거나 `True`로 기본 설정하지 않는다.

## 3-2. FRED / ALFRED 사용 정책

2026-11-06 기준 확인한 FRED 공식 약관에는 FRED 콘텐츠를 소프트웨어·시스템·머신러닝 개발 및 학습에 사용하는 제한이 있고, FRED API 추가 약관에는 FRED 서비스·콘텐츠의 저장·캐싱·아카이빙 및 별도 데이터베이스 통합에 관한 제한이 명시돼 있다. 데이터 시리즈의 원권리자가 제3자인 경우 별도 권리도 적용된다.

따라서 이 프로젝트의 기본 정책은 다음과 같다.

1. **FRED/ALFRED API 결과를 모델 학습용 Raw Store 또는 Feature Store에 적재하지 않는다.**
2. FRED/ALFRED에서 조회한 데이터를 장기 캐시·아카이브하거나 사용자에게 재배포하지 않는다.
3. 특정 목적의 사용이 허용되는지 모호하면 구현 전에 St. Louis Fed 및 원 데이터 권리자에게 서면 확인한다.
4. ML 학습 및 장기 보관이 필요한 데이터는 BLS, BEA, U.S. Treasury, Federal Reserve 등 원천기관에서 직접 제공하는 데이터와 해당 기관의 이용 조건을 개별적으로 확인한다.
5. 원천기관에서 직접 받았다는 이유만으로 저장·학습·재배포 권한이 자동으로 생긴다고 간주하지 않는다.
6. ALFRED의 빈티지 개념은 데이터 설계에 참고하되, FRED API 사용권한이 확인되지 않은 상태에서 ALFRED API를 학습용 빈티지 데이터 공급원으로 연결하지 않는다.
7. **과거 발표 당시 값(초판·빈티지)의 출처를 시리즈마다 따로 정한다.** 원천기관 API에서 직접 받아도 과거 빈티지가 자동으로 확보되지는 않는다(대개 최신 수정 값만 준다). 초판 값을 담은 허가된 출처(기관의 과거 발표 자료·실시간 데이터셋 등)를 계약으로 등록하고, 확보하지 못한 시리즈는 `REVISED_ONLY`로 표시해 학습·백테스트의 기본 피처에서 제외한다(7-2). 수정된 값으로 과거를 학습하면 그 시점에 몰랐던 정보를 쓰게 된다.

FRED 시계열을 개인 연구용으로 단순 조회·시각화하는 경우도 API 추가 약관, 시리즈별 저작권 및 출처표시 요건을 먼저 확인한다. 이 문서는 법률 자문을 대체하지 않는다.

## 3-3. 공급원별 기본 정책

| 데이터 | 후보 공급원 | PIT 요건 | 권한 확인 항목 |
|---|---|---|---|
| 한국 주가·거래량 | KRX 또는 라이선스 보유 공급자 | 거래소 세션, 수정 이력, 기업행위 | 저장·분석·화면 표시·재배포 |
| 한국 재무·공시 | OpenDART | 접수일, 정정공시, 보고서 버전 | API 이용조건, 상업적 활용, 원문 재배포 |
| 한국 거시 | 한국은행 ECOS 등 | 실제 발표·수정 가능성 | 저장·ML·표시·출처 |
| 미국 CPI·고용 | BLS 원천 API/발표자료 등 | 실제 발표일시, 초판·수정 이력 | API 약관, 데이터 보관, ML 학습 |
| 미국 GDP·개인소득 | BEA 원천 API/발표자료 등 | 발표 일정, 빈티지/수정 | API 약관, 데이터 보관, ML 학습 |
| 미국 국채금리 | U.S. Treasury 또는 승인 공급원 | 관측시점과 공개시점 구분 | 데이터 이용조건, 화면 표시 |
| 미국 주가·지수 | 거래소/라이선스 공급자 | 세션 마감 및 벤더 전달 시각 | 실시간·지연 데이터, 저장, ML, 표시, 재배포 |
| 산업·원자재·운임 | 산업 데이터 제공자/거래소/라이선스 공급자 | 기준시각·주기·수정 이력 | 상업 사용, 파생 데이터, 재배포 |
| 뉴스 | 허가된 뉴스 공급자/공식 공시 | 발행시각·수정·정정 | 원문 저장, 전문 재배포, LLM 입력, 요약·임베딩 |
| 애널리스트 컨센서스·추정치 | 라이선스 보유 데이터 공급자 | 추정치 스냅샷 시각, 개별 추정의 수정 이력, 집계 기준(평균·중앙값·추정 기관 수) | 저장·ML·표시·재배포, 원 작성 기관 권리 |

## 3-4. 후보 지표 사전: 정의와 의미를 분리

다음은 분석 후보이며, 모두 수집·학습 권한이 승인됐다는 뜻은 아니다.

| ID | 정확한 의미 | 주의사항 |
|---|---|---|
| DGS2 | 미국 2년 국채시장 수익률 | 일별 관측값의 공개·갱신 시점 확인 |
| DGS10 | 미국 10년 국채시장 수익률 | 동일 |
| DFII10 | 미국 10년 TIPS 실질수익률 | 명목금리와 구분 |
| FEDFUNDS | 월별 유효 연방기금금리 평균 | FOMC 목표금리 상·하단이 아님 |
| DFF | 일별 유효 연방기금금리 | 정책 목표 범위와 다름 |
| DFEDTARU / DFEDTARL | 연방기금 목표 범위 상단/하단 시리즈 | 정책 결정 이벤트 및 유효 시점 확인 |
| DTWEXBGS | 연준 광의 명목 달러지수 | ICE DXY와 동일하지 않음 |
| ICE DXY | ICE 산출 달러지수 | 별도 라이선스·공급원 확인 |
| DEXKOUS | 뉴욕 정오 기준 원/달러 환율 관측 시리즈 | KST 08:30 실시간 스냅샷이 아님. 원천인 연준 H.10은 주 1회(지난주 일별 값을 월요일) 공개되므로 관측일과 가용 시각이 며칠 차이 난다 |
| CPIAUCSL / CPILFESL | CPI / 근원 CPI 계열 | 월간 기준기간과 발표일·수정 이력 분리 |
| PAYEMS | 비농업 고용 수준 시리즈 | 월말 즉시 사용 불가, 발표일시 필요 |
| DCOILWTICO | WTI 현물가격 관련 시리즈 | 원유 선물 가격과 혼동 금지 |

미국 달러지수 `DTWEXBGS`를 `DXY`라는 이름으로 저장하지 않는다. 실제 ICE DXY를 쓰지 않는다면 UI와 모델 변수명에서도 `FED_BROAD_DOLLAR_INDEX`처럼 정확히 표기한다.

## 3-5. 데이터 공급원 상태 게이트

각 Contract는 검토 단계를 요약하는 상태 하나를 갖는다. **실제 허용 여부는 목적별 권한 필드(3-1)로만 판정한다.** 상태는 권한 필드를 넘어서는 권한을 주지 않으며, 검토 전(`NOT_REVIEWED`)과 `BLOCKED`는 모든 목적을 막는다. (구현: `core/data_contracts.py`의 `allows` = 상태가 열려 있고 그 목적의 권한 필드가 참)

- `NOT_REVIEWED`: 약관 검토 전 (모든 권한 필드는 거짓이어야 한다)
- `BLOCKED`: 약관 위반 또는 권한 미확인으로 사용 금지
- `RESEARCH_ONLY`: 허용된 개인·비상업 연구 범위에서만 사용
- `APPROVED_FOR_INTERNAL_USE`: 허용된 내부 분석·저장 가능
- `APPROVED_FOR_TRAINING`: 모델 학습·검증 가능
- `APPROVED_FOR_PUBLIC_DISPLAY`: 사용자 화면에 표시 가능
- `APPROVED_FOR_REDISTRIBUTION`: 원본 데이터 재배포 가능

하나의 상태가 다른 권한을 자동으로 포함하지 않는다. 예를 들어 내부 분석이 허용돼도 원본 데이터 공개나 LLM 입력이 허용되는 것은 아니다.

## 3-6. 데이터 커버리지와 분석 가능 상태

데이터 **권한**(3-1~3-5)과 데이터 **충분성**은 다른 문제다. 권한 게이트를 통과한 데이터라도, 분석 엔진이 요구하는 길이·주기·최신성을 채우지 못하면 그 엔진의 결과를 정상값처럼 내지 않는다.

모든 종목에 같은 최소 이력(예: 주가 252거래일, 재무제표 8분기)을 강제하지 않는다. 필요한 데이터는 **분석 엔진마다** 따로 정의한다. 신규 상장 종목이라 GARCH 예측은 할 수 없더라도, 확보된 재무제표로 하는 기본 기업 분석까지 막을 이유는 없다.

| 상태 | 의미 | 출력 |
|---|---|---|
| `SUFFICIENT` | 엔진에 필요한 데이터를 모두 충족 | 정상 산출 |
| `LIMITED` | 일부 데이터가 부족하지만 제한적 분석 가능 | 산출하되 제한 사항과 신뢰도 하향을 함께 표시 |
| `INSUFFICIENT` | 핵심 데이터가 부족해 산출 불가 | 값 대신 `INSUFFICIENT`와 부족한 입력 목록 |
| `STALE` | 데이터가 있으나 현재 판단에 쓰기에는 오래됨 (4-7 `max_staleness`) | 값 대신 `STALE`와 마지막 갱신 시각 |

| 분석 엔진 | 필요한 데이터 | 부족할 때 |
|---|---|---|
| GARCH / 변동성 | 충분한 가격 수익률 이력 | `INSUFFICIENT` 또는 `LIMITED`(신뢰도 낮음) |
| Rolling Beta | 일정 기간의 종목·시장 동시점 수익률 | 베타 `LIMITED` |
| Fair Value | 모델에 필요한 재무제표와 주식 수 | 해당 모델만 산출 보류 (10-2의 `N/A`) |
| Financial Health | 핵심 재무비율과 과거 변화 | 부분 평가(`LIMITED`) 또는 산출 불가 |
| Global Exposure | 사업부문별 매출과 산업 지표 | 부문별로 노출도 신뢰도 하향 (5-3) |
| Portfolio Risk | 동시점 종목별 수익률 표본 | 공분산 추정 불안정 경고 |

구현 원칙:

- 엔진별 요구 조건은 `core/coverage.py`의 `EngineRequirement`(입력별 최소 관측 수, 권장 관측 수, 최신성 정책)로 정의하고 버전을 기록한다.
- 기본 수치는 **초기 가정**이다. 실제 데이터로 추정 안정성(예: 표본 길이에 따른 GARCH 모수 분산)을 확인해 정하고, 바꾼 이유를 기록한다.
- 상태 판정은 `as_of` 시점에 이용 가능했던 데이터(4-2 PIT 조회 규칙)로만 한다. 미래에 채워질 데이터를 근거로 `SUFFICIENT`를 주지 않는다.
- 엔진의 모든 출력은 `coverage_status`와 판정 근거(부족한 입력, 마지막 관측 시각)를 함께 반환한다. UI·AI는 이 상태를 숨기지 않는다.
- 상태가 낮다고 결측을 임의 값으로 채워 `SUFFICIENT`로 바꾸지 않는다 (0-3의 Fail Closed).

---

# 4. Point-in-Time 데이터 모델 및 시간 정렬

## 4-1. 시간 필드 정의

다음 시각은 서로 다른 의미이므로 하나의 컬럼으로 합치지 않는다.

| 필드 | 의미 |
|---|---|
| `observation_period_start/end` | 데이터가 설명하는 경제·회계 기준기간 |
| `event_time_utc` | 실제 사건 또는 시장 관측이 발생한 시각 |
| `official_release_at_utc` | 원천기관의 공식 발표 시각(확인 가능할 때) |
| `vintage_date` | 빈티지 데이터에서 해당 값 버전이 유효해진 날짜 |
| `provider_available_at_utc` | 공급자 시스템에서 해당 값이 이용 가능해진 시각(확인 가능할 때) |
| `available_at_utc` | 백테스트 및 모델에서 사용을 허용하는 보수적 가용 시각 |
| `ingested_at_utc` | 우리 수집 시스템이 실제로 수집한 시각 |
| `revision_id` | 동일 관측값의 버전 식별자 |
| `supersedes_revision_id` | 이전 버전과의 연결 |

`available_at_utc`는 단순히 `observation_date` 또는 `vintage_date`를 복사해 만든 값이 아니다. 공식 발표 시각, 벤더 제공 지연, 데이터 확인 가능성, 보수적 적용 규칙을 통해 산출한다.

## 4-2. PIT 레코드 권장 스키마

```python
PIT_RECORD = {
    "entity_id": str,
    "series_id": str,
    "metric_id": str,
    "observation_period_start": "date | None",
    "observation_period_end": "date | None",
    "event_time_utc": "datetime | None",
    "official_release_at_utc": "datetime | None",
    "vintage_date": "date | None",
    "provider_available_at_utc": "datetime | None",
    "available_at_utc": "datetime",
    "ingested_at_utc": "datetime",
    "revision_id": str,
    "supersedes_revision_id": "str | None",
    "value": "numeric | str | bool | None",
    "unit": str,
    "currency": "str | None",
    "source_provider": str,
    "source_document_id": "str | None",
    "source_url": "str | None",
    "raw_payload_hash": str,
    "quality_status": str,
    "license_contract_id": str,
}
```

### PIT 조회 규칙

의사결정 시각 `as_of_utc`에서 사용할 수 있는 레코드는 다음 조건을 모두 만족해야 한다.

1. `available_at_utc <= as_of_utc`
2. 해당 데이터 계약이 현재 사용 목적(학습·평가·표시·LLM)에 승인되어 있다.
3. `quality_status`가 사용 가능한 상태다.
4. 동일 관측값의 여러 버전 중 `as_of_utc` 시점에 이용 가능했던 최신 버전만 선택한다.
5. 원본이 정정되더라도 과거 버전을 덮어쓰지 않는다.

`valid_from/valid_to`를 사용하는 경우에도 이는 시스템상 버전 유효 구간을 나타내며, 원천의 발표 시각을 대신하지 않는다.

## 4-3. DART 공시 가용시각

OpenDART `rcept_dt`는 `YYYYMMDD` 형식의 접수일자이며, 정확한 시·분·초로 간주하지 않는다.

**보수적 기본 규칙:**

- 정확한 공식 공개 시각과 공급자 가용 시각을 신뢰할 수 있는 출처로 확인할 수 없다면, 접수일 이후 다음 `XKRX` 거래 세션의 08:30 KST를 `available_at_utc`로 설정한다.
- `rcept_dt + 1 calendar day`를 사용하지 않는다. 토요일·일요일·공휴일을 고려한 다음 KRX 거래 세션을 계산한다.
- 정확한 공개 시각을 확보한 경우에는 의사결정 cutoff, 공시가 실제 이용 가능해진 시각, 벤더 지연을 모두 확인한 후 더 보수적인 시각을 사용한다.
- 정정공시는 별도 `revision_id`로 저장하고 기존 값을 덮어쓰지 않는다.
- 보고서 기준일과 공시 접수일을 별도 컬럼으로 보존한다.

예시 함수의 의미는 다음과 같다.

```python
def dart_available_at(receipt_date, kr_calendar, exact_publication_at=None):
    """정확한 공개시각이 없으면 접수일 이후 다음 KRX 세션 08:30을 반환한다.

    구현 시 exchange_calendars의 세션 라벨/타임존 규약을 확인하고,
    달력일과 거래 세션을 혼동하지 않는다.
    """
    if exact_publication_at is not None:
        # publication_at과 provider_available_at을 모두 검증한다.
        # 실제 가용 시각은 두 시각 중 늦은 시각이다.
        effective_available_at = max(
            exact_publication_at,
            validated_provider_available_at(exact_publication_at),
        )
        # 해당 시각 이후 처음 도래하는 한국 의사결정 cutoff를 사용한다.
        return first_kr_decision_cutoff_at_or_after(
            effective_available_at, kr_calendar, hour=8, minute=30
        )

    next_session = next_xkrx_session_after(receipt_date, kr_calendar)
    return session_cutoff(next_session, hour=8, minute=30, tz="Asia/Seoul")
```

**잠정실적과 확정실적:** 한국 기업은 정기보고서보다 몇 주 먼저 잠정 영업실적을 공정공시로 낸다. 잠정 값과 정기보고서의 확정 값은 같은 관측값(회사·지표·기간)의 서로 다른 버전으로 저장하고, 확정 값에 잠정 공시의 접수일을 붙이지 않는다(미래 정보 누출). 잠정 공시를 쓰지 않으면 정기보고서 접수 전까지 실적 피처가 한 분기 늦은 값이라는 점을 커버리지에 표시한다.

## 4-4. 거래소 캘린더 및 자산별 가용시각

### 원칙

- `America/New_York`와 `Asia/Seoul` 같은 IANA timezone을 사용한다. `EST` 또는 `EDT` 고정 오프셋을 하드코딩하지 않는다.
- 미국 현물시장 마감은 일반 세션과 조기폐장 세션을 거래소 캘린더에서 조회한다.
- `market_close_at`과 `provider_available_at`은 별도로 관리한다. 장 마감 시각에 최종 확정 데이터가 즉시 제공된다고 가정하지 않는다.
- NYSE·NASDAQ·CME 선물·FX 공급원은 동일한 세션 캘린더를 사용한다고 가정하지 않는다.
- 캘린더 라이브러리 버전을 고정하고, 주요 휴장일·조기폐장일을 공식 거래소 일정과 정기 대조한다.
- 한국의 정규장 외 거래 및 대체거래소 데이터는 거래 venue와 세션을 명시한다. `KRX 야간시세`처럼 공급원·시장구분이 불명확한 필드는 사용하지 않는다.
- 2025년 3월 4일 출범한 대체거래소 넥스트레이드(NXT)에서 프리마켓(08:00~08:50)과 애프터마켓(15:30~20:00)에 한국 주식이 거래된다. KRX 시가 단일가(08:30~09:00) 동안에는 NXT 거래가 멈춘다. 가격·거래량은 venue(KRX·NXT·통합)를 구분해 저장한다. 15:30 이후의 공시·미국 장 초반 정보는 같은 날 NXT 애프터마켓에서 이미 거래될 수 있다.
- KRX는 연초 첫 거래일과 대학수학능력시험일처럼 개장·마감 시각이 바뀌는 날이 있다. 고정 09:00을 가정하지 않고 거래소 캘린더의 특별 개장·마감 시각을 쓴다.

### 권장 테이블

```text
market_session
  venue_id
  session_label
  session_open_utc
  session_close_utc
  is_early_close
  calendar_version

series_observation
  series_id
  event_time_utc
  market_session_id
  provider_available_at_utc
  available_at_utc
  value
```

### 그룹별 정렬 규칙

모든 시리즈를 하나의 DataFrame으로 합쳐 `merge_asof` 한 번만 수행하지 않는다. **`entity_id + series_id` 그룹별로** 독립 정렬한다.
`series_id`만으로 묶으면 같은 지표 이름(예: `revenue`)을 쓰는 여러 회사의 값이 섞여, 삼성전자 매출이 SK하이닉스 행에 붙을 수 있다.
회사가 없는 거시 시리즈도 `entity_id`(예: 국가·발표 기관)를 채워 같은 규칙을 쓴다.

```python
aligned = pd.merge_asof(
    decision_grid.sort_values("decision_time_utc"),
    observations.sort_values("available_at_utc"),
    left_on="decision_time_utc",
    right_on="available_at_utc",
    by=["entity_id", "series_id"],
    direction="backward",
)
```

위 코드는 핵심 인터페이스 예시다. 실제 구현에서는 `decision_grid`에 각 그룹의 행이 존재하도록 만들고, 정렬 키·동일 타임스탬프·중복 revision·결측 정책을 명시한다.
구현: `core/calendar_align.py`의 `align_asof` (같은 그룹·같은 가용 시각의 중복 관측은 오류, 미매칭 행은 `NO_DATA`로 보존).

**필수 불변조건:**

```python
assert (aligned["available_at_utc"] <= aligned["decision_time_utc"]).all()
```

`NaT`와 미매칭 행은 조용히 제거하지 않고 별도 상태로 저장한다.

## 4-5. 한국 장기 휴장 중 미국 누적수익률

가장 최근 미국 종가를 복사하는 것만으로 휴장 중 누적 움직임이 보존되지는 않는다.

한국의 직전 의사결정 cutoff 이후 현재 cutoff까지 이용 가능해진 미국 거래 세션의 수익률을 별도 집계한다.

\[
R_{US, cumulative} = \prod_{j=1}^{n}(1+r_j)-1
\]

- 기준 시작 시점과 마지막 포함 세션을 명시한다.
- 한국 휴장 중 발생한 미국 세션을 중복 계산하지 않는다.
- **있어야 할 세션과 받은 종가를 대조한다.** 미국 거래소 캘린더로 그 구간의 예정 세션을 구하고, 하나라도 종가가 빠졌으면 남은 종가로 계산하지 않고 `INCOMPLETE`(값 없음)로 둔다. 결측이 큰 시장 움직임을 숨길 수 있기 때문이다. 결측 수익률을 0으로 채우거나 빼고 계산하지 않는다.
- 시작 기준가는 직전 cutoff 이전 마지막 미국 세션의 종가다. 그 종가가 빠졌으면 더 오래된 종가로 대신하지 않는다.
- 미국 휴장이라 구간에 예정 세션이 없을 때만 수익률 0과 `NO_US_SESSION`을 쓴다.
- USD 수익률과 원화 환산 수익률을 별도 변수로 보존한다.
- 환율은 해당 미국 종가 시각 또는 별도 FX 관측시각과 맞춘다. 서로 다른 시점의 환율과 주가를 조합할 때는 시간 차이를 메타데이터로 표시한다.

## 4-6. 개장 전 브리핑의 시간 컷오프

브리핑은 하나의 시각에 모든 데이터가 완성된다고 가정하지 않는다. 초기 운영안은 다음과 같이 두 단계로 설계한다.

| 단계 | 목표시각(KST) | 사용할 수 있는 데이터 | 출력 상태 |
|---|---:|---|---|
| 1차 브리핑 | 08:00 | 전일 미국 현물 종가, 공식 발표가 완료된 거시지표, 공급자 전달이 확인된 데이터 | `PRELIMINARY` |
| 최종 업데이트 | 08:35 | 08:30 이전까지 공급자가 실제 제공한 환율·선물 등 추가 스냅샷 | `FINAL_PREOPEN` 또는 `PARTIAL` |
| 장 시작 | 09:00 | 신규 의사결정·실행 시점 | 별도 실행 정책 적용 |

08:30 KST 컷오프는 NXT 프리마켓(08:00~08:50) 도중이다. 그 시각에는 한국 주식의 장전 가격이 이미 존재하므로, 이 가격을 피처로 쓰려면 venue·관측시각·권한을 계약에 등록하고, 쓰지 않으면 그 정보를 쓰지 않았다는 사실을 기록한다.

08:25 스냅샷을 사용할 경우에는 해당 공급자의 실제 관측시각·시장 세션·가용시각이 검증되어 있어야 한다. 공급자가 08:25 값을 제공하지 않거나 권한이 없으면 해당 피처를 제외하고 데이터 공백을 표시한다. 브리핑마다 `snapshot_cutoff_utc`, `generated_at_utc`, `last_source_update_at_utc`, `coverage_status`를 저장한다.

## 4-7. `max_staleness` 정책

모든 지표에 `max_ffill_days=10`을 일괄 적용하지 않는다.

- 일별 거래 데이터: 거래소 세션 기준으로 마지막 관측 이후 누락 세션 수를 확인한다.
- 월간 CPI: 예정된 다음 발표일과 공급자 상태를 기준으로 오래된 데이터 여부를 판단한다.
- 분기 재무제표: 공시 주기·기업별 연기·정정공시를 고려한다.
- 주간 데이터: 해당 통계의 발표 주기를 따른다.

각 계약에는 `expected_frequency`, `expected_next_release_policy`, `max_staleness_policy_id`, `staleness_status`를 기록한다.

정책은 **레코드의 `license_contract_id`로 계약을 찾아** 적용한다. `series_id`(예: `CPIAUCSL`)로 계약을 추측하지 않는다. 계약 ID와 시리즈 ID는 서로 다른 이름 공간이라, 추측하면 계약을 못 찾고 기본값(예: 10일)이 조용히 적용된다. 계약을 못 찾으면 기본값으로 대신하지 않고 `UNKNOWN_POLICY`로 막는다(Fail Closed).

---

## 4-8. 종목 식별자 매핑 (Security Master)

`entity_id`는 내부 식별자다. KRX 종목코드, ISIN, DART 고유번호(`corp_code`), 해외 상장 티커를 **시점별로** 연결하는 매핑 테이블을 둔다.

- 매핑 행마다 유효 시작·종료일과 출처(계약 ID)를 둔다. 종목코드 변경, 합병·분할, 상장폐지·재상장, 지주회사 전환을 새 행으로 남기고 과거 행을 덮어쓰지 않는다.
- 과거 시점의 조회는 그 시점에 유효했던 매핑을 쓴다. 현재 종목코드로 과거 데이터를 묶지 않는다.
- 시장마다 회계 기준·거래소 캘린더·세금·통화·식별 체계가 다르므로 시장별 어댑터는 따로 두고, 매핑 테이블에서만 연결한다.

# 5. 글로벌 데이터 및 산업별 피처 설계

## 5-1. 글로벌 시장 데이터

후보 시장지표:

- S&P 500, NASDAQ 100, Russell 2000
- Philadelphia Semiconductor Index(SOX)
- VIX
- 미국 국채 2Y·10Y, 10Y 실질금리
- 달러지수(광의 지수와 ICE DXY 구분)
- USD/KRW(뉴욕 정오 관측값과 한국 개장 전 스냅샷 구분)
- 승인된 원자재·산업 지표

미국 지수·개별 주식의 인덱스 데이터는 데이터 제공자와 상품별 권한을 확인한다. `^SOX`와 `SOXX`는 같은 자산이 아니다. ETF를 프록시로 쓰면 `proxy_type=ETF`로 저장하고 SOX 지수로 표시하지 않는다.

## 5-2. 산업별 글로벌 드라이버

산업 매핑은 연구 가설이지 인과관계가 입증된 표가 아니다. 각 변수는 `equity_proxy`, `physical_price`, `supply_demand`, `macro_exposure`, `policy_event` 등 유형을 구분한다.

| 한국 산업 | 주식·ETF 프록시 후보 | 산업 실물·수급 변수 후보 | 해석 주의점 |
|---|---|---|---|
| 반도체 | SOX, NVDA, MU, TSM, AMAT 등 | DRAM/NAND 가격, HBM 공급·수요, 글로벌 반도체 매출, 장비 투자 | 메모리·파운드리·장비·팹리스 노출을 구분 |
| 자동차 | 미국 자동차 OEM/ETF, 글로벌 완성차 | 미국 신차 판매, 재고, 인센티브, 전기차 판매, 관세·정책 | TSLA 주가 하나로 한국 완성차 수요를 대표하지 않음 |
| 배터리 | ALB, SQM, LIT 등 | 리튬·니켈 가격, 셀 가격, EV 판매량, 배터리 출하량 | LIT는 배터리 관련 기업 주식 ETF 프록시이지 리튬 현물가격이 아님 |
| 조선 | 글로벌 조선·해운 관련 기업/ETF | 신규 선박 발주, 수주잔고, 신조선가, LNG선 발주, 인도량 | BDRY는 건화물 운임 노출 프록시이며 조선 수주 지표가 아님 |
| 철강·금속 | 글로벌 철강·광산 기업 | 철광석·구리·니켈 가격, 중국 PMI, 생산량 | 기업 주식 프록시와 금속 현물·선물가격 구분 |
| 금융 | 글로벌 은행/금융 ETF | 금리곡선, 신용스프레드, 연체율·대손비용, 규제 이벤트 | 금리 상승 효과는 예대마진·평가손익·자금조달에 따라 다름 |

업종 단위 표만으로는 여러 사업을 하는 기업을 설명하지 못한다. 드라이버는 **사업부문 단위**로 정의하고 5-3의 노출도 모델로 기업에 연결한다. 아래는 후보 예시이며, 각 변수는 데이터 계약(3-1)과 Model A/B/C 실험(7장)을 통과해야 쓴다.

| 사업부문 예시 | 글로벌 산업 드라이버 후보 | 비고 |
|---|---|---|
| 메모리 반도체 | DRAM·NAND 고정·현물가격, 대만 반도체 수출, MU | 가격 지표의 공표 시점·주기 확인 |
| 파운드리 | TSMC 월매출, 글로벌 반도체 수요 | 월매출 발표일 기준으로 PIT 정렬 |
| 모바일 | 글로벌 스마트폰 출하량, 주요 스마트폰 기업 실적 | 분기 출하 통계의 발표 지연 |
| 조선 | 신조선가지수, LNG선 발주·운임, 수주잔고, 인도량 | BDI 하나로 조선 업황을 대표하지 않음 |
| 배터리 | 글로벌 EV 판매량, 리튬·니켈 가격, 배터리 출하량 | 리튬 ETF와 리튬 가격 구분 |

## 5-3. 사업부문별 글로벌 노출도 모델

고정된 업종 티커만으로 기업을 설명하지 않는다. 기존 구조(기업 → 업종 지표, 예: 삼성전자 → 반도체 → SOXX·NVDA)는 스마트폰·가전처럼 다른 사업의 영향을 놓친다.
v2.2는 **기업 → 사업부문 → 글로벌 산업지표**의 2단계로 연결하고, 부문별 영향을 기업 전체로 합친다.

```text
Company (예: 삼성전자)
 ├─ Business Segment: 메모리   ── DRAM 가격, MU, 대만 반도체 수출
 ├─ Business Segment: 파운드리 ── TSMC 월매출, 반도체 수요
 ├─ Business Segment: 모바일   ── 글로벌 스마트폰 출하, 주요 기업 실적
 │   각 부문: Revenue Share (PIT) · Operating Income Share (PIT) · Region Exposure (PIT)
 │            · Capacity / Utilization · Backlog / Shipments · Industry Drivers · Driver Coverage
 └─ Company-level Aggregation (부문별 영향의 가중 합, 부문별 신뢰도 보존)
```

사업부문마다 다음 정보를 관리한다.

| 정보 | 분석 목적 |
|---|---|
| 사업부문별 매출 비중 | 기업 매출 구조 파악 |
| 사업부문별 영업이익 비중 | 실제 수익 기여도 파악 (매출 비중과 크게 다를 수 있음) |
| 지역별 매출 노출도 | 미국·중국·유럽 등 수요 노출 파악 |
| 생산능력 및 가동률 | 공급 측면 분석 |
| 수주잔고 및 출하량 | 미래 매출 가시성 분석 (조선·방산·장비 등) |
| 산업별 글로벌 지표 | 외부 산업 환경 분석 (5-2) |

규칙:

- **공시 시점 기준(PIT)으로만 쓴다.** 사업부문 비중은 그 비중이 실린 보고서의 가용 시각(4-3 DART 규칙)부터 유효하다. 2026년 사업보고서의 비중을 2025년 분석에 소급 적용하지 않는다. 사업부문 재편으로 부문 이름·범위가 바뀌면 새 버전으로 저장하고 옛 버전을 덮어쓰지 않는다.
- 기업별 사업부문 비중을 코드 안의 고정 딕셔너리로 두지 않는다. 과거 사업구조를 잘못 재현한다.
- 매출 가중과 영업이익 가중 노출도를 둘 다 계산해 보고한다. 영업이익이 음수인 부문이 있으면 영업이익 가중은 `LIMITED`로 표시한다.
- 부문별 비중이 공개되지 않거나 비교할 수 없으면 추정치를 사실처럼 쓰지 않는다. `exposure_confidence=LOW` 또는 `UNAVAILABLE`로 표시한다.
- **부문별 분석 신뢰도를 따로 보여 준다.** 같은 반도체 기업이라도 메모리 업황 데이터는 충분하고(`SUFFICIENT`), 파운드리 수요 데이터는 부족하고(`LIMITED`), 환율은 오래됐을(`STALE`) 수 있다. 기업 전체 노출도를 한 점수로 표시하기 전에 부문·드라이버별 커버리지(3-6)를 함께 제시한다.
- 글로벌 드라이버가 실제 예측에 기여하는지는 Model A/B/C 실험으로 검증한다. 업종 단위 노출(Model C-industry)과 사업부문 단위 노출(Model C-segment)을 같은 표본에서 비교하고, 기여가 없으면 그대로 보고한다.

**구현 (2026-11):** 데이터는 OpenDART 재무제표 원본(XBRL)의 영업부문·지역 주석에서 숫자만 추출한다(계약 `opendart_xbrl_v1`, 원본 zip은 저장하지 않음).
실제 DART XBRL 점검에서 확인한 구조와 대응:

- 거의 모든 숫자 사실에 연결·별도 차원이 붙는다 → 연결 기준을 먼저 쓰고, 없으면 별도 기준을 쓰되 `LIMITED`.
- 같은 부문이 영업부문 표 외에 공사계약·수익 분해 표에도 나온다 → 멤버 이름에 담긴 표 이름으로 영업부문 표를 고른다.
- 영업이익 대신 부문 이익(당기순이익 등)만 공시하는 회사가 있다 → `segment_profit`으로 구분하고 이익 가중은 `LIMITED`.
- 단일 부문 회사는 부문 표가 없다 → 추출 상태를 레코드로 남기고 노출도는 `INSUFFICIENT`(추정하지 않음).
- 부문→산업 연결은 레이블 키워드 규칙(`config/segment_taxonomy.json`, 버전 관리)으로 하고, 연결되지 않은 부문은 `UNMAPPED`로 표시한다.
- 코드: `collectors/opendart_xbrl.py`(해석), `collectors/opendart_segments.py`(추출·PIT 레코드), `feature_store/segment_exposure.py`(노출도).

권장 레코드:

```python
SEGMENT_EXPOSURE_RECORD = {
    "entity_id": str,
    "segment_id": str,              # 회사 내부 부문 ID (재편 시 새 ID 또는 매핑 버전)
    "segment_name": str,
    "metric_id": str,               # revenue_share | operating_income_share | region_share:<region> | capacity | utilization | backlog | shipments
    "value": float,
    "unit": str,
    "fiscal_period_end": "date",
    "consolidation": str,           # 연결 | 별도
    "source_document_id": str,      # 예: DART 접수번호
    "available_at_utc": "datetime", # 4-3 규칙으로 산출
    "revision_id": str,
    "supersedes_revision_id": "str | None",
    "license_contract_id": str,
}
```

## 5-4. 피처 엔지니어링

권장 피처 후보:

1. 미국 지수·산업지표의 1D/5D/20D 수익률
2. 한국 시장 의사결정 시점까지 누적된 미국 오버나이트 수익률
3. 동일 통화 기준 및 환율 환산 수익률
4. VIX 수준·변화율·롤링 Z-score
5. 미국 2Y/10Y 금리 및 장단기 스프레드
6. 미국 실질금리 변화
7. 환율 변화·변동성·미국 지수와의 디커플링
8. 한국 종목과 글로벌 드라이버 간 롤링 베타·상관계수
9. 업종 상대강도와 글로벌 피어 상대강도
10. 기업 실적·재무 데이터의 PIT TTM 성장률 및 품질 피처

모든 롤링 통계·표준화·직교화·PCA·결측 대체·피처 선택은 각 워크포워드 학습 구간에서만 fit한다. 전체 표본으로 계산한 통계량을 과거 구간에 역적용하지 않는다.

`Orthogonalized Beta`는 시장 베타 효과를 제거한 잔차를 의미할 때만 사용한다. 잔차가 인과적 산업 노출도를 의미한다고 주장하지 않는다.

## 5-5. 시장 국면(Regime) 설계

상호 배타적이지 않은 상태를 하나의 단일 라벨로 강제하지 않는다. 다음 축을 독립적으로 추정한다.

| 축 | 상태 예시 | 입력 후보 |
|---|---|---|
| Market Risk | Risk-On / Neutral / Risk-Off | 주가 추세, VIX, 신용스프레드 |
| Monetary Policy | Easing / Neutral / Tightening | 목표금리, 기대금리, 실질금리 |
| Inflation | Inflationary / Stable / Disinflationary | CPI, 근원 CPI, 임금, 원자재 |
| Semiconductor Cycle | Expansion / Neutral / Contraction | SOX, 메모리 가격, 반도체 매출·CAPEX |
| FX Environment | KRW Strengthening / Neutral / Weakening | 승인된 USD/KRW 관측치, DXY |

Markov Switching 등 상태모델 사용 시 실시간·백테스트 피처에는 **Filtered Probability**만 사용한다. 전체 표본 미래 정보를 포함하는 Smoothed Probability는 과거 예측 피처로 사용하지 않는다.

임계값은 사전에 고정하거나 각 학습 구간에서만 추정한다. 전체 표본을 본 뒤 유리한 임계값을 고르고 같은 표본에서 성과를 보고하지 않는다.

---

## 5-6. 공시 이벤트 분석 (Event Study)

뉴스 원문은 쓰지 않는다(라이선스, 부록 A). 이용 조건을 확인한 DART 공시 목록(`opendart_list_v1`)만으로 이벤트를 정의한다.

| 이벤트 유형 (예) | 공시 식별 | 주의 |
|---|---|---|
| 유상증자·전환사채·신주인수권부사채 | 보고서명 (`유상증자결정`, `전환사채권발행결정` 등) | 발행 조건 정정, 철회(비고 `철`) |
| 자기주식 취득·소각 | `자기주식취득결정`, `주식소각결정` 등 | 신탁 계약과 직접 취득 구분 |
| 수주·공급계약 | `단일판매ㆍ공급계약체결` | 계약 규모를 매출 대비 비율로 정규화 |
| 잠정실적 | 잠정 영업실적 공정공시 | 시장 기대 대비 차이는 컨센서스 계약(3-3) 없이는 계산하지 않음 |

규칙:

- **이벤트 시각은 공시의 가용 시각(4-3)이다.** 접수일을 이벤트일로 쓰지 않는다. 장중·장후 공시는 다음 거래 가능 시점부터 반응을 잰다. 넥스트레이드 애프터마켓(4-4)에서 이미 반응했을 수 있다는 점을 표시한다.
- 정정공시(`[기재정정]`)는 같은 사건의 반복으로 세지 않고, 철회된 공시는 이벤트에서 빼되 기록은 남긴다.
- 초과수익은 사전에 정한 기준(시장모형, 업종 지수 차감 등)과 창(예: [0,+1], [0,+5], [0,+20] 거래일)으로 계산한다. 결과를 본 뒤 창이나 기준을 바꾸지 않는다.
- 같은 날·같은 회사에 겹친 이벤트, 시장 전체 급변일에 몰린 이벤트(군집)를 표시하고, 표본 수와 신뢰구간을 함께 보고한다.
- 이벤트 피처를 예측 모델에 넣을 때는 가용 시각 이후에만 값이 생기도록 PIT로 만든다. 이벤트 분석 결과 자체를 근거로 종목을 추천하지 않는다(7-13).

# 6. 데이터 저장소 및 파이프라인

## 6-1. 초기 저장 구조

초기 개인 프로젝트에서는 Parquet + DuckDB를 기본 분석 저장소로 사용한다. PostgreSQL은 사용자 계정·관심종목·포트폴리오·API 서비스 등 트랜잭션 요구가 발생할 때 도입한다. TimescaleDB는 실제 쿼리·운영 요구로 필요성이 확인될 때 선택한다.

```text
storage/
├── raw/                  # 저장 권한이 확인된 원본 응답만
│   ├── kr/
│   ├── us/
│   ├── macro/
│   └── news_filings/
├── normalized/
├── pit_versions/
├── features/
│   ├── research/
│   └── production/
├── forecasts/
├── model_artifacts/
├── experiments/
└── manifests/
```

민감한 API 키, 비공개 라이선스 데이터, 개인 사용자 정보는 Git 저장소에 업로드하지 않는다.

## 6-2. 원본 및 가공 데이터의 추적

각 수집 배치는 다음 메타데이터를 남긴다.

- `source_id`, `contract_id`, `request_id`
- `collected_at_utc`, `provider_available_at_utc`
- HTTP 상태·페이지네이션·API 버전
- 원본 페이로드 해시(SHA-256)
- 변환 코드 버전·스키마 버전
- 수집 건수·중복·결측·파싱 오류
- 사용 권한 상태와 출처 표시 조건

권한이 없는 데이터는 `raw/`에 보관하지 않는다. 보관이 허용되는 경우에도 변경 불가 원본과 정규화 데이터를 분리한다.

## 6-3. 수집 클라이언트

`collectors/base_client.py`의 필수 기능:

- Token Bucket 또는 공급자별 Rate Limit 관리
- HTTP 429 및 `Retry-After` 처리
- 지수 백오프와 Jitter
- idempotent 수집과 중복 제거
- 페이지네이션 완결성 확인
- 응답 스키마 변화 감지
- 실패·재시도·최종 상태 로깅
- 권한이 허용된 경우에만 캐시 및 원본 보관

## 6-4. 파이프라인 오케스트레이션

MVP에서는 GitHub Actions 또는 간단한 단일 스케줄러 중 하나를 선택한다. 여러 오케스트레이터를 동시에 도입하지 않는다.

작업은 다음 단계로 나눈다.

```text
ingest_sources
    ↓
validate_raw
    ↓
normalize_and_version
    ↓
build_pit_features
    ↓
run_forecast
    ↓
write_forecast_audit
    ↓
run_quality_and_drift_checks
    ↓
render_briefing
```

각 단계는 재실행 가능해야 하며, 실패한 단계 이후의 작업은 입력 산출물과 상태가 확인될 때만 실행한다.

---

# 7. 모델 A/B/C 연구 설계 및 성능 검증

## 7-1. 연구 가설

핵심 질문은 다음과 같다.

> 과거 특정 시점에 실제로 이용 가능했던 글로벌 데이터가 한국 주식의 미래 수익률·방향·위험 예측에 한국 단독 데이터보다 추가 정보를 제공하는가? 그 정보가 거래비용과 한국 시장 체결 제약을 반영한 뒤에도 경제적으로 유의미한가?

Model C가 Model A를 이겨야 한다고 전제하지 않는다.

## 7-2. 모델 피처셋

| 모델 | 입력 피처 | 목적 |
|---|---|---|
| Model A | 한국 주가·거래량·수급·PIT 재무·국내 거시 | 국내 데이터 기준선 |
| Model B | Model A + 미국 광역 지수·거시·금리·환율 | 글로벌 시장·거시 정보의 추가 기여 |
| Model C | Model B + 업종별 승인된 글로벌 드라이버·다축 Regime 상호작용 | 산업별 글로벌 정보의 추가 기여 |

초판 값을 확보하지 못한 거시 시리즈(`REVISED_ONLY`, 3-2)는 Model B/C의 기본 피처에서 제외하고, 넣는다면 별도 실험으로만 보고한다.

Model A/B/C는 동일한 공통 표본에서 먼저 비교한다. 글로벌 데이터가 없는 기간까지 포함한 확장 표본 결과는 별도 표로 보고한다. 두 결과를 혼합해 우월성을 주장하지 않는다.

## 7-3. 예측 타깃 사전 정의

각 타깃은 실험 전에 고정한다. 예시:

| Target ID | 예측 대상 | 정의 예시 |
|---|---|---|
| `RET_1D` | 다음 거래일 수익률 | 의사결정 cutoff 이후 실제 진입 가능 가격부터 다음 세션 기준 청산 가격까지 |
| `RET_5D` | 5거래일 누적수익률 | 진입 가격과 5거래일 후 청산 가격으로 정의 |
| `RET_21D` | 21거래일 누적수익률 | 진입 가격과 21거래일 후 청산 가격으로 정의 |
| `DIR_1D/5D/21D` | 방향 | 해당 기간 누적수익률이 0 초과인지 여부 등 사전 정의 |
| `RISK_1D/5D` | 하방위험 | 보유기간의 VaR/ES 또는 사전 정의한 손실 타깃 |
| `RANK_21D` | 횡단면 순위 | 같은 리밸런싱 시점 유니버스 내 미래 수익률 순위 |

08:30 KST 신호를 사용한다면 진입 가격은 다음에 실제 거래 가능한 가격(예: 시초가 또는 명시된 실행 가정)으로 정한다. 어느 venue의 어느 가격인지(KRX 시가 단일가, NXT 등)를 타깃 정의에 적는다. 해당 가격이 체결됐다고 볼 수 없는 상황은 백테스터의 미체결 규칙에 따른다.

## 7-4. 유니버스 및 생존편향

- 1차 재현: 기존 35개 종목으로 과거 스냅샷 결과와 비교
- 2차 확장: 각 과거 시점의 KOSPI 구성 또는 사전에 정의한 KOSPI 유니버스
- 3차 확장: KOSDAQ 유니버스
- 상장폐지·거래정지·관리종목·신규상장·티커 변경 이력을 PIT로 관리
- 미래의 지수 구성 종목을 과거 유니버스에 소급 적용하지 않음
- 유동성 하위 종목 필터는 과거 해당 시점까지 계산 가능한 ADV로 결정

## 7-5. 워크포워드 및 정보 누출 방지

- Expanding 또는 Rolling Window를 사전에 정의한다.
- 예측 타깃 기간이 겹치는 경우 Purged Split과 Embargo를 적용한다.
- 모든 전처리(결측 대체, 스케일링, 윈저라이징, 직교화, PCA, 피처 선택, 하이퍼파라미터 선택)는 학습 폴드 안에서만 fit한다.
- 학습·검증·최종 홀드아웃 구간을 구분한다.
- 최종 홀드아웃은 개발 중 반복 조회해 튜닝하는 용도로 사용하지 않는다.
- 같은 시점의 횡단면 종목을 무작위로 학습·테스트에 분리해 미래 정보가 섞이지 않게 한다.

## 7-6. 비교 기준선

Model A/B/C 외에 다음의 단순 기준선을 포함한다.

1. Historical Mean 또는 Historical Median
2. 항상 상승/항상 하락이 아닌 무조건부 상승확률 기준
3. Buy & Hold
4. 단순 Momentum
5. Equal-Weight Factor Strategy
6. Logistic Regression 또는 Ridge 등 단순 모델

복잡한 모델이 단순 기준선보다 나은지 확인한다.

## 7-7. 모델 복잡도와 피처 통제

- 동일한 알고리즘 패밀리와 동일한 하이퍼파라미터 탐색 예산을 사용한다.
- Model C의 글로벌 피처는 업종별 후보군에서 학습 구간 내부 선택으로 제한한다.
- 글로벌 변수 3~5개 제한은 초기 실험 상한일 뿐 보편적으로 최적이라는 가정은 하지 않는다.
- `SOX`, `MU`, `NVDA`, 환율 등은 상관이 높을 수 있으므로 중복 정보와 다중공선성을 확인한다.
- 직교화 잔차의 기준 회귀와 학습 시점을 명시한다.

## 7-8. 평가 지표 및 통계 검정

### 통계적 예측력

- MAE, RMSE, Out-of-Sample R²
- Directional Accuracy 및 무조건부 상승률 대비 차이
- Brier Score, Log Loss, Calibration Curve
- Rank IC, ICIR
- 분위수·하방위험 예측의 Coverage 및 Quantile Loss

### 전략 성과

- Net CAGR, Net Sharpe, Sortino
- Maximum Drawdown
- Turnover
- 비용 차감 후 벤치마크 초과수익
- 체결 실패·현금 비중·시장충격 민감도
- DSR 및 여러 전략 선택에 따른 과최적화 평가
- 알려진 요인(시장·규모·가치·모멘텀 등)으로 회귀한 뒤의 알파와 HAC 표준오차. 요인 노출로 설명되는 수익을 실력으로 보고하지 않는다.
  - 한국 요인은 직접 만든다. 공개 요인 라이브러리에는 한국 단독 요인이 없다. 이 저장소의 PIT 시세·재무 데이터로 계산하고, 재무 값은 가용 시각(4-3) 이후에만 쓴다. 분할 기준점·재구성 시점·유니버스(상장폐지 포함) 규칙을 버전으로 기록한다.

### 국면별·기간별 성과 분해

전체 기간 평균만 보고하지 않는다. 상승장·하락장, 금리 상승기, 위기 구간 등 국면(5-5)과 하위 기간별로 성과와 Model A/B/C 차이를 나눠 보고한다.

- 국면 라벨도 PIT로 정한다. 사후에 알게 된 국면 구분으로 과거를 나누지 않는다.
- 국면마다 표본 수를 함께 적고, 표본이 적은 국면의 결과로 결론을 내리지 않는다.
- 국면 전환 모형을 쓰면 전환 확률과 상태 유지 기간을 함께 진단한다.

### 적절한 모델 비교 검정

- Diebold-Mariano: 비교할 예측 손실 시계열과 가정이 적합할 때 적용
- Clark-West: 중첩 예측모형 비교에 적합한 조건에서 적용
- Block Bootstrap: 시계열 의존성을 고려한 신뢰구간·차이 검정
- White Reality Check 또는 Hansen SPA: 다수 전략 탐색에 따른 선택 편향 평가
- PBO(Probability of Backtest Overfitting)·CSCV: 여러 후보 중 고른 전략이 표본 밖에서 중간 이하로 떨어질 확률. Strategy Lab(7-12)처럼 후보가 많을 때 DSR·SPA와 함께 본다
- Multiple Testing Correction: 사전 정의한 가설군과 다중 비교 수를 반영
- Hansen-Hodrick 또는 Newey-West 등 HAC: 해당 통계량의 의존 구조와 겹치는 수익률 구간에 맞게 선택

어떤 한 가지 검정도 모든 문제를 해결하지 않는다. 검정 선택 이유, 표본 수, 유의수준, 효과 크기, 신뢰구간을 결과와 함께 기록한다.

## 7-9. Experiment Registry 및 Model Registry

각 실험은 자동으로 다음 항목을 기록한다.

```python
EXPERIMENT_RECORD = {
    "experiment_id": str,
    "created_at_utc": "datetime",
    "git_commit": str,
    "environment_lock_hash": str,
    "data_snapshot_id": str,
    "data_manifest_hash": str,
    "feature_schema_version": str,
    "feature_list_hash": str,
    "universe_snapshot_id": str,
    "target_definition_id": str,
    "fold_definition": dict,
    "model_family": str,
    "hyperparameters": dict,
    "preprocessing_pipeline_version": str,
    "cost_model_version": str,
    "metrics": dict,
    "statistical_tests": dict,
    "artifacts": list[str],
    "notes": str | None,
}
```

Model Registry는 실험 레코드와 별도로 학습된 아티팩트의 `model_id`, 학습 데이터 스냅샷, 학습 기간, 모델 파일 해시, 파라미터, 라이브러리 버전, 평가 결과, 승인 상태, 배포 상태를 관리한다. 등록되지 않은 모델 파일을 프로덕션 UI 또는 AI 도구에서 호출하지 않는다.

실험 기록은 관리용 장부가 아니라 **통계 판정의 입력**이다. 같은 질문을 몇 번, 어떤 변형으로 시험했는지가 다중검정 보정(DSR의 시행 수, Reality Check/SPA의 후보 집합)에 들어간다. 개발자 실험과 사용자의 Strategy Lab 실험을 같은 원칙으로 기록한다 (7-12).

## 7-10. Forecast Audit Trail

예측·시그널은 생성 시점에 Append-only 방식으로 저장한다. 사후 실현값은 별도 평가 레코드로 추가하고 원래 예측 레코드를 수정하지 않는다.

예측 레코드는 "삼성전자 예상 수익률 +5%" 같은 결과만이 아니라, **그 예측으로 실제 거래할 수 있었는지**를 판단할 정보까지 연결한다.

```python
FORECAST_RECORD = {
    "forecast_id": str,
    "created_at_utc": "datetime",
    "decision_time_utc": "datetime",
    "data_cutoff_utc": "datetime",            # 어느 시점까지의 정보로 판단했는가
    "entry_price_assumption": str,            # 예: NEXT_OPEN | NEXT_CLOSE | VWAP_0900_0930
    "entry_tradability": dict,                # 거래정지·가격제한·거래량 제약 점검 결과
    "holding_period_sessions": int,           # 1, 5, 21 등
    "ticker": str,
    "universe_snapshot_id": str,
    "feature_snapshot_id": str,
    "feature_hash": str,
    "model_id": str,
    "target_definition_id": str,
    "target_horizon": int,
    "entry_execution_assumption": str,
    "predicted_return": float | None,
    "predicted_probability": float | None,
    "prediction_interval": dict | None,       # 예: {"68": [lo, hi], "95": [lo, hi]}
    "fair_value_band": dict | None,
    "risk_metrics": dict | None,
    "signal_status": str,
    "abstained": bool,
}

FORECAST_EVALUATION = {
    "forecast_id": str,
    "evaluated_at_utc": "datetime",
    "fill_type": str,                         # ACTUAL(증권사 체결 기록) | SIMULATED(백테스터 가정)
    "entry_price": float | None,
    "exit_price": float | None,
    "fill_status": str,                       # FILLED | PARTIAL | NOT_FILLED | NOT_TRADABLE
    "cost_breakdown": dict | None,            # 수수료·세금·슬리피지
    "return_gross": float | None,
    "return_net": float | None,
    "label_available_at_utc": "datetime | None",
    "evaluation_status": str,
    "failure_attribution": dict | None,       # 크게 틀린 예측의 원인 후보: 업종 급락·환율·확률 과대평가·국면 전환 등
}
```

규칙:

- **예측 정확도와 투자 성과를 분리해 평가한다.** 방향을 맞혔더라도 체결할 수 없는 가격(상한가 대기열, 거래정지)이었다면 경제적 성과는 다르다. 정확도 평가는 타깃 정의(7-3)의 실현값으로, 투자 성과 평가는 체결 가능성과 비용을 반영한 체결 가격으로 따로 기록한다.
- 증권사 주문과 연결하지 않은 시스템의 체결은 모두 `SIMULATED`다. 화면·보고서에서 실제 체결처럼 표시하지 않는다.
- 예측 타깃의 실현값이 아직 발생하지 않았거나 데이터가 확정되지 않았다면 `PENDING`으로 남긴다.
- **소급 기록 금지:** `created_at_utc`가 `data_cutoff_utc` 다음 거래 세션 시작 이후라면 그 예측은 기록하지 않는다. 다음 날 결과를 알 수 있는 시점에 적은 예측은 실전 기록이 아니다 (이전 저장소에서 예약 실행 지연과 데이터 누락으로 실제로 발생한 문제).

## 7-11. No-Edge 기준

`No-Edge`는 모델이 통계적으로 우수하지 않거나, 표본이 부족하거나, 데이터 품질이 불충분한 상태를 뜻한다.

- 단기 방향 모델의 No-Edge는 단기 방향 시그널을 보류한다.
- 단기 예측력이 없다는 이유만으로 장기 기업가치 분석까지 부정하지 않는다.
- 통계적 유의성만으로 실전 투자 가능성을 인정하지 않는다. 비용·체결·위험 조정 성과를 함께 본다.
- 반복적으로 종목을 선별해 가장 좋은 결과만 공개하는 행위를 금지한다.
- Strategy Lab에서 찾은 전략은 Final OOS Vault 평가(7-12)를 통과하기 전까지 검증된 신호가 아니라 `탐색 결과`로 표시한다.

## 7-12. Strategy Lab 실험 기록과 Final OOS Vault

모델 개발자뿐 아니라 **플랫폼 사용자도 과적합을 만든다.** 사용자가 Strategy Lab에서 수수료·팩터 가중치·리밸런싱 주기를 수백 번 바꾸면, 결국 검증 표본에 맞춘 전략을 고르게 된다. 따라서 데이터 구간과 실험 기록을 다음처럼 나눈다.

```text
1. Development / Exploration   사용자가 자유롭게 실험하는 구간
                               (전략 조합·팩터·수수료·리밸런싱 변경)
        │  모든 실험 자동 기록
        ▼
2. Validation                  모델·전략을 비교하고 고르는 구간
                               (실험 횟수와 파라미터 탐색 이력 관리)
        │  사전 등록한 공식 후보만 이동
        ▼
3. Locked Final OOS Vault      최종 검증 구간
                               (일반 사용자의 반복 조회·튜닝 차단)
```

규칙:

- **모든 실험을 자동 보관한다.** 실험마다 사용자·세션, 종목·유니버스 스냅샷, 기간, 팩터, 파라미터, 거래비용, 기준 모델, 결과 요약, 데이터 스냅샷 ID를 기록한다. 사용자가 지울 수 없다.
- Vault 구간의 데이터는 Exploration·Validation에서 조회할 수 없다. 화면·API·AI 도구 모두 같은 차단을 받는다.
- Vault 평가는 사전 등록한 공식 후보(가설·파라미터·채택 기준을 평가 전에 기록)만 받는다. 평가 요청과 결과는 횟수와 함께 기록하고, 같은 후보를 조정해 반복 평가하지 않는다.
- 반복 실험 기록은 다중검정과 선택 편향 평가에 쓴다. 단, 실험 횟수를 그대로 DSR의 시행 수에 넣지 않는다. 서로 상관된 변형(예: 수수료만 다른 전략)은 독립 시행이 아니므로, 전략 수익률 상관에서 추정한 **유효 시행 수**와 탐색 집합 전체에 대한 Reality Check/SPA를 함께 쓴다.
- 사용자 화면에는 "이 전략을 찾기까지 시도한 실험 수, 보정 뒤 유의성, Vault 평가 여부"를 함께 표시한다.
- 이전 저장소(Quant_Finance_Project_KOR)의 `EXPERIMENTS.md` 사전 등록(후보와 채택 규칙을 결과 전에 커밋하고 그 규칙으로만 판정)을 개발자용 Vault 절차의 기준선으로 삼는다.

---

## 7-13. 전략 모의 포트폴리오 기록

"추천 종목"을 공개하지 않는다. 대신 **사전 등록한 전략의 모의 포트폴리오를 앞으로의 기록으로만** 남긴다. 목적은 투자 권유가 아니라 전략이 실제로 통하는지에 대한 검증이다.

- 전략 정의(유니버스, 순위 규칙, 종목 수, 리밸런싱 주기, 비용 가정)를 기록 시작 전에 고정하고 버전을 붙인다. 바꾸면 새 전략으로 기록을 새로 시작한다.
- 리밸런싱마다 편입·제외 종목, 결정 시각, 데이터 컷오프, 진입 가정을 기록하고, 장 시작 이후에는 그날 기록을 추가하거나 고치지 않는다 (7-10의 늦은 기록 금지와 같은 규칙).
- 성과는 비용 차감 후 벤치마크와 비교하고, 체결 가능성(8-2)과 모의 체결(`SIMULATED`)을 표시한다.
- 화면에는 "검증 기록이며 투자 권유가 아님"을 표시하고, 유료 제공이나 개인별 맞춤 제공은 하지 않는다. 공개·유료화 전에는 유사투자자문업 해당 여부를 법률 검토한다(15-1).
- 이전 저장소(Quant_Finance_Project_KOR)의 주간 종목 고르기 기록이 이 방식의 기준선이다.

# 8. 한국 시장 백테스터 및 체결 모델

## 8-1. 가격 데이터 분리

다음 데이터를 구분한다.

- Raw/Unadjusted OHLCV: 실제 가격제한폭·호가단위·체결 가정
- PIT Corporate Action Events: 배당·분할·유무상증자·권리락·합병 등
- Point-in-Time Adjusted Price / Total Return: 해당 시점에 이용 가능한 조정 이력을 기준으로 산출

현재 공급자가 제공하는 최종 수정주가를 과거 전체 기간에 무조건 적용하지 않는다. 기업행위의 발표일·효력일·조정계수를 별도로 기록한다.

## 8-2. 가격제한폭 및 거래 가능성

일반적인 KOSPI·KOSDAQ 주식에는 기준가격 대비 ±30% 가격제한폭이 적용되지만, 정리매매·ELW·신주인수권증권·레버리지 ETF 등 예외가 있다. 시장·상품·시점에 따른 실제 규정을 사용한다.

- `±29.5%` 같은 임의 임계값을 전 종목에 적용하지 않는다.
- 기준가격과 호가가격단위에 따라 실제 상한가·하한가를 계산한다. 가격제한폭 금액의 호가단위 미만 처리는 **기준가격에 해당하는 호가가격단위**로 한다는 지적이 있다(v2.1 검토). 상·하한가 각각의 가격대 호가단위를 쓰는 구현과 결과가 다를 수 있으므로, 구현 전에 KRX 업무규정 시행세칙 원문으로 확인하고 경계 사례(호가단위 구간이 바뀌는 가격)를 테스트로 고정한다.
- 호가가격단위는 날짜별 표를 쓴다. 2022년 11월 발표된 개편(가격대별 단위 축소, 유가증권·코스닥·코넥스 단위 통일)은 2023년 1월 차세대 시스템과 함께 시행될 예정이었다. 실제 시행일과 구간은 KRX 공지로 확인해 그 전후에 다른 표를 적용한다.
- 거래정지, 관리종목, 단일가매매, VI, 신규상장 및 기업행위 관련 거래조건을 구분한다.
- 가격제한폭 도달과 주문 미체결은 동일하지 않다. 실제 체결은 반대 주문과 잔량에 달려 있다.
- 일별 OHLCV만으로 상한가 대기열과 실제 체결 가능성을 복원할 수 없다. 불확실한 경우 보수적 미체결·부분체결 시나리오를 함께 보고한다.
- "상한가 도달 시 매수 0% 체결" 같은 규칙은 사실이 아니라 **보수적 체결 가정**이다. 결과와 화면에 가정으로 표시하고, 가정을 바꾼 민감도를 함께 보고한다.

## 8-3. 슬리피지 및 시장충격

초기 시장충격 후보식:

\[
Impact_{bps}=BaseSpread_{bps}+\eta\cdot\sigma_{daily}\cdot\sqrt{OrderNotional/ADV_{20}}
\]

이 식은 초기 근사식이며 실제 시장충격 모델로 확정된 것이 아니다. 구현 전에 다음을 정의한다.

- `sigma_daily`: 소수형 일간 변동성인지, 백분율인지
- `ADV_20`: 주식 수 또는 거래대금 중 어떤 단위인지
- `eta`: 종목·시장·주문 유형별 추정 방법과 단위. 추정에 쓴 데이터도 PIT로 관리하고, 추정 구간 이후의 데이터로 과거 비용을 계산하지 않는다
- 주문 참여율 상한과 비유동성 구간 처리
- Base Spread 추정 방식
- 슬리피지·시장충격의 최대값 및 민감도 시나리오

시장 데이터가 충분하지 않으면 고정 bp와 유동성 기반 시나리오를 병렬 제시하되, 실제 체결 성능으로 과장하지 않는다.

## 8-4. 수수료·세금·포지션 제약

- 세금은 연도별·시장별·상품별 적용 규칙을 PIT로 관리한다.
- 매수·매도 수수료와 거래세의 적용 대상을 분리한다.
- 기본 전략은 Long-Only / Cash를 가정한다.
- 공매도는 별도 borrow availability, 대차 비용, 업틱룰·시장제약 및 당시 규칙을 구현하지 않는 한 실전 가능 성과로 표시하지 않는다.
- 백테스트에는 현금 잔고, 주문 수량, 부분체결, 미체결, 리밸런싱 시점, 체결가격이 포함돼야 한다.

## 8-5. 실행 시나리오

- Base: 명시된 주문·체결 가정
- Conservative: 보수적 슬리피지·부분체결·미체결 비율
- Stress: 유동성 급락·가격제한폭·거래정지 시나리오

최종 성과는 한 가지 낙관적 체결 가정만으로 보고하지 않는다.
백테스터의 체결 결과는 Forecast Audit(7-10)에 `fill_type=SIMULATED`로 기록되며, 어떤 시나리오의 체결 가정인지 함께 남긴다.

---

# 9. Quant Risk 및 포트폴리오 사이징

## 9-1. GARCH 출력 정의

- GARCH/GJR-GARCH는 조건부 변동성 및 분포를 추정한다.
- Student-t 등 잔차 분포 가정, 평균 방정식, 추정창, 재추정 주기를 기록한다.
- 미래 수익률 또는 가격의 불확실성 범위는 `Prediction Interval`로 표시한다.
- `Prediction Interval`은 실현값의 분포 범위이지 모수의 `Confidence Interval`과 다르다.
- 다기간 예측은 분포의 경로·복리 구조를 고려해 계산하고, 단순 일간 변동성 곱셈으로 모든 구간을 대체하지 않는다.
- 추정에 필요한 수익률 이력이 부족하면 커버리지 상태(3-6)를 `INSUFFICIENT` 또는 `LIMITED`로 반환한다. 신규 상장 종목에 짧은 이력으로 추정한 변동성을 정상값처럼 표시하지 않는다.

## 9-2. VaR / ES 부호 규약

시스템 내부에서는 손실을 양수로 정의한다.

- `VaR_95 = 0.03`: 95% 신뢰수준에서 손실 분위가 자산의 3%라는 의미
- `ES_95 = 0.045`: 해당 꼬리 영역의 평균 손실이 자산의 4.5%라는 의미
- `exceedance = realized_loss > VaR`

UI에서 수익률 부호로 표시할 경우 별도 변환 규칙을 적용한다. VaR/ES의 기간(1D, 5D), 신뢰수준, 통화, 포트폴리오 기준을 반드시 표시한다.

Kupiec POF 및 Christoffersen 검정은 VaR 예외의 빈도·독립성/조건부 커버리지를 진단하기 위한 것이며, 단일 예외 또는 연속 예외를 자동 손절 명령으로 해석하지 않는다. 표본 수와 검정력의 한계를 함께 표시한다.

## 9-3. 세 가지 리스크 기능 분리

| 모듈 | 목적 | 핵심 입력 |
|---|---|---|
| Volatility Targeting | 포트폴리오 예상 변동성을 목표 수준에 맞춤 | 기초 비중, 예측 공분산, 목표 변동성 |
| ES / Risk Budget | 포트폴리오 손실 예산 및 종목별 위험 한도 관리 | ES, 위험 예산, 집중도 한도 |
| Risk Parity / Risk Contribution | 공분산을 고려해 위험 기여도를 배분 | 비중 벡터, 공분산 행렬 |

역변동성 가중치 계산은 Risk Parity와 동일하지 않다. 문서와 코드에서 구분한다.

## 9-4. Volatility Targeting

먼저 전략의 기본 비중 `w_base`를 정하고, 예측 포트폴리오 변동성을 이용해 전체 비중을 조정한다.

\[
\sigma_{p,forecast}=\sqrt{w_{base}^{\top}\Sigma_{forecast}w_{base}}
\]

\[
\lambda=\min\left(1,\frac{\sigma_{target}}{\sigma_{p,forecast}}\right)
\]

\[
w_{scaled}=\lambda w_{base}
\]

그다음 종목별·업종별·시장별 최대 비중, 현금 비중, 유동성, 거래비용 및 ES 위험 예산 제약을 적용한다.

종목별 ES 제한을 단순히 `L_max / ES_i`로 계산하는 방식은 종목 간 상관관계를 반영하지 못하므로 초기 단일 종목 상한 근사에 한정한다. 포트폴리오 전체 위험은 전체 수익률 분포 또는 공분산·꼬리 의존성을 고려해 재계산한다.

## 9-5. Risk Parity 및 Marginal Risk

포트폴리오 변동성:

\[
\sigma_p=\sqrt{w^\top\Sigma w}
\]

종목별 변동성 위험기여도:

\[
RC_i=\frac{w_i(\Sigma w)_i}{\sigma_p}
\]

Risk Parity는 위험기여도 균형을 목표로 최적화한다. 거래비용, 공매도 금지, 종목·업종 최대 비중, 유동성 제약을 포함한다.

- 종목별 위험기여도에 상한(예: 25%)을 두고 넘으면 비중을 깎는 방식은 **위험기여도 상한 관리**이지 Risk Parity가 아니다. Risk Parity는 목표 위험기여도와 현재 위험기여도의 차이를 줄이는 최적화로 구하고, 두 기능을 이름부터 구분한다.
- 결과 검증 (실패하면 비중을 내지 않고 오류로 보고):
  - 최적화 수렴 여부. 반복 한도에 도달했으면 수렴으로 간주하지 않는다.
  - 공분산 행렬의 대칭·양의 준정부호. 표본이 부족하면 수축 추정 등을 적용한 뒤 다시 확인하고, 적용한 방법을 기록한다.
  - 비중 벡터와 공분산 행렬의 종목 순서·크기 일치.
  - 비중(현금 포함) 합 = 1. 반올림은 출력 직전에만 하고 반올림 오차는 현금에 반영한다.

VaR/ES 기반 위험기여도는 분포 가정과 계산 방법이 달라질 수 있으므로 변동성 Risk Contribution과 별도로 정의한다.

## 9-6. 신호와 사이징의 결합

`has_edge=False`는 해당 예측 전략의 방향성 포지션을 생성하지 않는다는 의미로만 사용한다. 장기 기업가치 분석이나 재무 건전성 점수 전체를 무효화하지 않는다.

`signal_synthesizer`는 신호 상태를 만들고, `portfolio_sizing_engine`은 전략별 목표 비중과 제약을 계산한다. 단일 Boolean을 곱해 모든 장기 포지션을 0으로 만드는 구조를 사용하지 않는다.

---

# 10. Fair Value Range Engine

## 10-1. 기본 원칙

Fair Value는 관측된 정답이 아니라 재무 전망·할인율·비교기업·시나리오에 의존하는 추정값이다. 따라서 `Valuation Range` 또는 `Scenario-based Fair Value Band`로 표시한다. 이를 통계적 `Prediction Interval`과 혼동하지 않는다.

## 10-2. 모델 적용 가능성

| 기업 유형 | 주요 모델 | 보조 모델/주의 |
|---|---|---|
| 일반 제조업 | FCFF DCF, PER, EV/EBITDA | PBR, RIM, 경기 중간값 정상화 |
| 성장 기술기업 | 시나리오 DCF, EV/Sales, 정상화 PER | 현금흐름 음수·희석·성장 가정 민감도 |
| 은행 | RIM, PBR, ROE/자기자본비용 | 일반 EV/EBITDA 적용 주의 |
| 보험 | RIM, PBR, 배당모형, 자본적정성 분석 | 보험계약·자본규제 특성 고려 |
| 증권 | PBR·RIM, 수수료·운용·이자 손익 분해 | 시장 거래대금·금리·자본규제 민감도 |
| 안정 배당기업 | DDM, FCFE DCF, 배당수익률 비교 | 배당 정책의 지속가능성 확인 |
| 적자·초기 기업 | 시나리오 기반 EV/Sales, 자산·현금 소진 분석 | 근거 없는 영구 DCF 단일 적용 금지 |
| 지주회사 | SOTP(상장 자회사는 시가, 비상장 자회사는 별도 평가, 순현금·순차입 반영), NAV 할인율 | 할인율의 근거와 시기별 변화 기록, 자회사 가치 이중 계산 주의 |
| 경기순환 업종 (철강·화학·조선·메모리 등) | 정상화 이익 기반 PER·EV/EBITDA, 사이클 국면별 시나리오 | 정점·저점 이익을 영구화하지 않음. 정상화 구간의 정의와 버전 기록 |

모델이 적합하지 않거나 입력값이 부족하면 `N/A`로 반환한다. 4개 모델을 모든 기업에 강제로 적용하지 않는다. 입력 부족에 따른 `N/A`는 커버리지 상태(3-6)와 부족한 입력을 함께 기록한다.

## 10-3. DCF의 수학적 일관성

### FCFF 방식

- FCFF는 기업 전체 자본 제공자에게 귀속되는 현금흐름이다.
- FCFF는 WACC로 할인해 Enterprise Value를 계산한다.
- Enterprise Value에서 순차입금, 우선주, 비지배지분 등 적절한 Equity Bridge 항목을 조정해 주주가치로 변환한다.

### FCFE 방식

- FCFE는 보통주 주주에게 귀속되는 현금흐름이다.
- FCFE는 Cost of Equity로 할인해 Equity Value를 직접 계산한다.

### WACC 및 Cost of Equity

\[
WACC=\frac{E}{D+E}R_e+\frac{D}{D+E}R_d(1-T)
\]

\[
R_e=R_f+\beta\cdot ERP
\]

필요 시 추가 프리미엄을 사용할 수 있지만 근거·추정방식·중복 여부를 기록한다. 무위험금리, 현금흐름, 할인율은 동일한 통화 및 명목/실질 기준으로 일치시킨다.

Terminal Growth는 할인율보다 낮아야 하며, 통화·물가·장기 경제성장 가정과 일관되어야 한다. 성장에는 재투자가 필요하므로 영구성장률과 재투자율을 함께 정한다(예: g = 재투자율 × 신규 투자수익률). 재투자 없이 높은 성장률을 두지 않는다. 한국 명목 GDP 이하라는 단일 고정 규칙을 모든 기업에 기계적으로 적용하지 않는다.

## 10-4. Equity Bridge 및 주식 수

- 현금·현금성 자산
- 이자부 부채
- 리스부채 등 사업모델에 적합한 부채 조정
- 비영업자산
- 우선주·비지배지분
- 희석 가능 주식 수 및 주식기준보상

회계 데이터가 연결/별도인지, 분기/누적인지, 통화·단위가 무엇인지 기록한다.

## 10-5. Fair Value Band 산출

권장 시나리오:

- Bear
- Base
- Bull

각 시나리오에 매출 성장, 마진, 재투자율, WACC/Cost of Equity, 영구성장률, 주식 수 등 가정을 저장한다.

`fair_value_low/mid/high`는 시나리오와 모델 적합성에 기반해 산출한다. 시나리오 확률 가중 평균을 쓰면 시나리오 확률의 근거를 함께 기록한다. 단순히 여러 모델의 숫자를 평균 내거나 이상치를 임의로 클리핑하지 않는다.

- 모델별 계산 가능 여부와 적용 이유 표시
- 가중치의 산정 규칙과 버전 기록
- 동일 기초 가정을 공유하는 모델 간 중복 정보 검토
- 민감도 표와 주요 가정 공개
- 입력 부족 또는 모델 간 분산이 과도하면 `valuation_status=UNRELIABLE/INSUFFICIENT_DATA` 반환

### 사용자 화면: Fair Value 민감도

적정가치를 하나의 정답처럼 보여 주지 않는다. **어떤 가정에서 가치가 얼마나 달라지는지**를 투자자가 직접 확인하게 한다.

- 기준 가치(Base)와 가치 범위(Bear~Bull)를 함께 표시한다.
- 할인율(WACC 또는 Cost of Equity)과 영구성장률을 축으로 한 민감도 표를 제공한다. 필요하면 매출 성장률·마진 축도 추가한다.
- 업종별로 어떤 평가모델을 적용했는지, 다른 모델은 왜 제외했는지(10-2 적용 가능성, 입력 부족)를 함께 보여 준다.
- 각 셀의 값은 같은 엔진으로 다시 계산한 값이다. 화면에서 보간하거나 임의로 반올림한 값을 계산 결과처럼 표시하지 않는다.
- 민감도 축의 범위(예: 할인율 ±1%p, 영구성장률 ±0.5%p)는 고정된 정답이 아니다. 기업·업종의 가정 불확실성에 맞춰 정하고 그 근거를 기록한다. 점수 가중치(예: 재무 건전성 40/40/20)도 검증 없이 고정하지 않는다.

화면 형태 예시 (임의 숫자, 실제 기업의 평가 결과가 아님):

| 할인율 변화 | 성장률 변화 | 적정가치 |
|---|---|---|
| 기준 −1%p | 기준 | 95,000원 |
| 기준 | 기준 | 80,000원 (Base, 범위 65,000~95,000원) |
| 기준 +1%p | 기준 | 69,000원 |

---

# 11. Corporate Financial Health / Investment Attractiveness / Market Risk

## 11-1. 점수 체계 분리

하나의 점수에 서로 다른 개념을 모두 넣지 않는다.

### A. Corporate Financial Health

기업 자체의 재무적 건강 상태를 측정한다.

- 수익성 및 자본 효율성
- 현금흐름의 질
- 부채·유동성·이자상환 능력
- 재무 성장과 안정성
- 회계 품질 관련 보조 플래그

### B. Investment Attractiveness

현재 투자 분석 대상으로서의 매력도를 측정한다.

- Fair Value 대비 가격 수준
- 동종업계 상대가치
- 실적 전망 변화
- 모멘텀 및 상대강도
- 글로벌 산업 노출도

### C. Market Risk

주가 및 시장 위험을 측정한다.

- GARCH 변동성
- VaR/ES
- Maximum Drawdown
- 시장·산업 베타
- 유동성·환율·글로벌 리스크 노출도

## 11-2. F/Z/M Score의 적용 제한

- **Piotroski F-Score:** 9개 회계 기반 기준을 계산한다. 원래 연구의 표본·목적과 한국 기업·업종 전체에 대한 적용 가능성을 구분하고, 데이터 정의·회계 기준을 검증한다. 금융업 등 부적합한 업종에는 무리하게 적용하지 않는다.
- **Altman Z-Score:** 제조업·비제조업 등 기업 유형별 적합한 변형식을 사용한다. 은행·보험 등 금융회사에는 일반 제조업 모델을 그대로 적용하지 않고 업권별 지표를 쓴다: 은행은 CET1·BIS 비율, 고정이하여신(NPL) 비율, 순이자마진(NIM), 대손비용률, 보험은 지급여력(K-ICS) 비율과 보험계약마진, 증권은 순자본비율과 레버리지.
- **Beneish M-Score:** 회계상 이상 징후를 선별하는 경고 지표다. 분식회계를 확정하거나 기업의 부정을 단정하는 데 사용하지 않는다. `-1.78` 등 문헌상 임계값은 보편적 판정 규칙이 아니라 참고 기준으로 표시하고 한국 시장에서 검증한다.

## 11-3. 점수 정규화

- 같은 시점의 같은 업종·비교군 안에서 PIT 데이터로 백분위를 계산한다.
- 수익성 지표와 위험·부채 지표의 선호 방향을 명확히 정의한다.
- 극단치에 Robust Scaling 또는 사전 정의한 Winsorization을 적용하되 학습·평가 구간을 분리한다.
- 결측값을 중립 점수로 임의 대체하지 않는다.
- 업종 표본 수가 부족하면 상위 업종군으로 확장하거나 `INSUFFICIENT_PEER_SAMPLE` 반환.
- 점수마다 커버리지 상태(3-6)를 함께 반환한다. 일부 재무 항목만 있으면 `LIMITED`로 부분 평가하고, 어떤 항목이 빠졌는지 표시한다.
- 글로벌 산업 노출도가 점수에 들어갈 때는 사업부문별 신뢰도(5-3)를 보존해 보여 준다. 낮은 신뢰도의 부문이 점수를 좌우하면 그 사실을 표시한다.
- 동일한 재무 지표가 F-Score와 종합 점수에 중복 반영되는 영향을 보고한다.
- 0~100 점수는 확률이 아니다. 화면에 "점수(업종 내 백분위)"로 표시하고 "상승 확률 82%"처럼 읽히지 않게 한다.
- 점수 가중치와 업종별 매핑 버전을 저장한다.

---

# 12. Quant Signal 및 추천 상태

## 12-1. 추천 기간 분리

단기 예측력이 없다고 장기 기업가치 분석까지 무효화하지 않는다.

| 분석 유형 | 핵심 근거 |
|---|---|
| Short-term Quant Signal | 1~5일 OOS 예측 성능, 비용, 단기 위험 |
| Swing Horizon | 5~21일 OOS 예측·모멘텀·유동성 |
| Fundamental Watchlist | Fair Value, 재무건전성, 산업 전망 |
| Long-horizon Research | 장기 가치·현금흐름·사업부문·산업 구조 |

## 12-2. 상태값

```python
class QuantSignalStatus(str, Enum):
    VALIDATED_SIGNAL = "validated_signal"
    FUNDAMENTAL_CANDIDATE = "fundamental_candidate"
    WATCHLIST = "watchlist"
    NEUTRAL = "neutral"
    CAUTION = "caution"
    NO_EDGE = "no_edge"
    INSUFFICIENT_DATA = "insufficient_data"
    HIGH_RISK = "high_risk"
    DATA_STALE = "data_stale"
    CIRCUIT_BREAKER_ACTIVE = "circuit_breaker_active"
```

`Tier 1` 등의 표현을 사용하더라도 어떤 점수 또는 통계 기준에 해당하는지 정의해야 하며, 매수·매도 권유 또는 수익 보장으로 오해될 수 있는 표현은 규제 검토 후 결정한다.

## 12-3. 시그널 카드 필수 정보

- 시그널 상태와 적용 투자 기간
- 데이터 기준시각 및 데이터 품질 상태
- 사용 모델·버전·피처 스냅샷
- 예측 기대수익 또는 예측확률(검증된 경우에만). 확률로 표시하려면 보정 품질(Brier Score·Calibration Curve)을 함께 보인다
- Prediction Interval과 VaR/ES의 기간·신뢰수준
- Fair Value Band와 핵심 가정
- 긍정·부정 근거
- 판단 무효화 조건 및 조건의 데이터 출처
- 검증 기간, OOS 성능, 표본 수, 불확실성
- 적용된 체결·비용 가정

`Invalidation Trigger`는 검증된 논리 또는 사전에 명시한 조건에 근거해야 한다. 임의 가격 또는 VaR 예외 횟수를 기계적으로 손절 기준으로 사용하지 않는다.

---

# 13. Portfolio Intelligence

## 13-1. 필수 기능

- 사용자 입력 보유종목·수량·평균단가(개인 데이터는 별도 접근통제)
- 종목·업종·국가·통화별 집중도
- USD/KRW 환산 손익과 통화 노출도
- 공분산 기반 포트폴리오 변동성
- Portfolio VaR/ES
- Marginal/Component Risk Contribution
- 유동성·거래비용·최대 비중 제약
- Volatility Targeting과 Risk Parity의 별도 분석
- 포트폴리오 성과: 비용 차감 수익률, Sharpe, 최대 낙폭, 벤치마크 대비 (보유 주식·ETF 포함)

## 13-2. 주의사항

- 일별 종목 수익률이 충분하지 않은 경우 공분산 추정의 불안정성을 표시한다.
- 상관관계가 위기 시 급변할 수 있으므로 Stress Scenario를 별도로 제시한다.
- 비정상·결측 가격 및 거래중단 구간을 단순 0 수익률로 치환하지 않는다.
- 사용자 보유 자산을 읽는 기능은 동의·인증·암호화·보관기간 정책을 별도로 정의한다.
- 사용자별 맞춤형 매수·매도 판단을 제공하는 기능은 법률 검토가 끝나기 전 활성화하지 않는다.

---

# 14. Evidence-Grounded AI Assistant

## 14-1. 역할 분리

| 도구 | 기능 | 실행 제한 |
|---|---|---|
| `query_pit_financials` | PIT 재무·비율 조회 | 파라미터화된 읽기 전용 조회 |
| `run_quant_analysis` | 예측·위험·베타 조회 | 승인된 코어 엔진만 호출 |
| `run_valuation` | Fair Value Band 조회 | 지정된 모델 및 PIT 재무만 사용 |
| `search_dart_filings` | 공시 검색 | `available_at <= as_of` 필터 강제 |
| `search_market_news` | 뉴스 하이브리드 검색 | 라이선스·발행시각·원문 접근 권한 확인 |
| `compare_companies` | 기업 비교 | 동일 기준일·동일 단위로 정규화 |
| `screen_universe` | 조건별 종목 검색 | 허용된 필터 스키마만 실행 |

LLM에 임의 SQL, Python 코드 실행 권한 또는 외부 쓰기 권한을 제공하지 않는다.

## 14-2. 숫자·주장 검증

숫자가 Tool JSON에 존재하는지만 검사하지 않는다. 각 주장에는 구조화된 근거를 연결한다.

```python
class EvidenceClaim:
    claim_id: str
    claim_text: str                  # 예: "삼성전자 영업이익 증가"
    metric_id: str | None            # 예: operating_income
    value: float | str | None        # 예: 10조
    unit: str | None
    currency: str | None
    period_type: str | None          # QUARTER | ANNUAL | TTM | YTD
    fiscal_period: str | None        # 예: 2026Q3
    consolidation: str | None        # 연결 | 별도
    as_of_utc: str | None
    methodology_id: str | None       # 계산 방식 (예: 연결 영업이익 기준, 전년 동기 대비)
    tool_call_id: str | None         # 이 수치를 돌려준 도구 호출
    source_ids: list[str]            # 예: DART 접수번호
    evidence_spans: list[str]        # 공시의 해당 표·문단 위치
    status: str                      # VERIFIED | WARNING | ABSTAINED
```

검증 항목:

- 값의 정확한 일치
- 지표명·분모·분자·산식의 의미 일치
- 단위·통화·백분율·배수의 일치
- 기준일·관측 기간·발표일 일치
- 데이터 출처 및 원문 위치 확인
- 계산값이 어떤 엔진·버전에서 나왔는지 확인
- 근거가 부족하면 `abstained=True` 처리

숫자가 일치한다고 검증이 끝난 것이 아니다. 다음과 같은 **의미 불일치**도 `WARNING` 또는 `ABSTAINED`로 잡는다.

- 영업이익을 순이익이라고 설명하는 경우 (지표 혼동)
- 분기 수치를 연간 수치라고 설명하는 경우 (기간 유형 혼동)
- 별도 기준 수치를 연결 기준이라고 설명하는 경우
- 퍼센트(%)와 퍼센트포인트(%p), 증가율과 증가액을 섞는 경우
- 도구 호출 결과에 없는 기준일을 붙이는 경우

`VERIFIED`는 값·지표 의미·기간·기준·출처·원문 위치가 모두 맞을 때만 준다. 일부만 확인되면 `WARNING`으로 표시하고, 핵심 근거가 없으면 `ABSTAINED`로 답을 보류한다.

가능한 경우 중요 수치는 자유서술에 맡기지 않고 검증된 수치 카드 또는 템플릿으로 렌더링한다.

투자 판단과 관련된 답변은 반대 근거(위험 요인, 불리한 지표, 모델의 약한 부분)를 함께 제시한다. 근거 없이 한쪽 방향의 결론만 내지 않는다.

## 14-3. RAG 및 Prompt Injection

- 원문 문서·뉴스를 신뢰할 수 없는 데이터로 격리한다.
- 검색된 문서가 시스템 지침·도구 권한·사용자 권한을 바꾸지 못하도록 한다.
- 특정 키워드 제거만으로 Prompt Injection 방어가 완료되었다고 간주하지 않는다.
- 읽기 전용 allowlist 도구만 노출하고 SQL은 파라미터화한다.
- 문서 접근 권한과 LLM 입력 권한을 별도로 확인한다.
- 공시의 `published_at`뿐 아니라 `available_at` 기준 PIT 필터를 적용한다.
- RAG 결과에 문서 ID, 출처 URL, 발행일, 검색된 문단 위치를 보존한다.

## 14-4. AI 평가 세트

Golden Question Set을 만들어 다음을 주기적으로 평가한다.

- 수치 일치율
- 출처 연결 정확도
- 기준일 정확도
- 검색 재현율·정밀도
- 잘못된 근거로 결론을 내리는 비율
- 답변 거절의 적절성
- 프롬프트 인젝션 성공률
- 도구 선택 오류 및 권한 위반

`Zero-Hallucination`을 품질 목표로 표방하지 않는다. `Evidence-Grounded AI with Measurable Error Control`을 목표로 하며, 평가 결과와 오류 유형을 기록한다.

---

# 15. 규제·보안·개인정보·운영 통제

## 15-1. 금융 규제 검토

한국 자본시장법상 서비스의 분류는 화면의 용어만으로 결정되지 않는다. 서비스가 제공하는 내용, 대가 수취 여부, 일방향·양방향 여부, 개인화 수준, 종목·가격·수량·시기 판단 제공 여부, 영업 형태 등을 함께 검토해야 한다.

따라서 다음을 금지한다.

- `Quant Signal Tier`라는 명칭을 사용했다는 이유로 규제 검토를 생략하는 것
- 면책문구만으로 투자자문·유사투자자문 관련 의무가 면제된다고 주장하는 것
- 개인별 자산·투자목적에 따른 매수·매도 또는 비중 판단을 규제 검토 없이 제공하는 것
- 검증되지 않은 가상 백테스트 수익률을 실현 수익률처럼 홍보하는 것

외부 사용자에게 유료 서비스 또는 개인화 기능을 제공하기 전에 국내 금융규제 전문 변호사 또는 자격 있는 전문가의 서면 검토를 받는다. 미국 등 타 국가 사용자를 대상으로 할 경우 해당 관할의 규제도 별도로 검토한다.

## 15-2. 보안

- API 키는 환경변수 또는 Secret Manager로 관리한다.
- `.env`, 토큰, 라이선스 파일, 개인 사용자 데이터는 Git에 커밋하지 않는다.
- 인증(Authentication)과 권한(Authorization)을 분리한다.
- 사용자 데이터는 최소 수집·최소 보관 원칙을 적용한다.
- 로그에 API 키·계정정보·민감한 포트폴리오 정보를 기록하지 않는다.
- 백업·복구·삭제 정책을 정의한다.
- AI 도구는 최소 권한 원칙과 읽기 전용 권한으로 시작한다.

## 15-3. 데이터 무결성 Circuit Breaker와 Market Risk Control 분리

### Data Integrity Circuit Breaker

다음 상황에서 해당 데이터 또는 분석을 중단한다.

- PIT 시간 역전
- 잘못된 단위·통화·종목코드
- 스키마 변경 또는 파싱 실패
- 공급자 데이터 지연·결측 임계치 초과
- 중복·비정상 값의 원인이 데이터 오류로 확인된 경우
- 사용 권한이 차단 또는 만료된 경우

### Market Risk Control

다음 상황에서는 분석을 유지하되 위험 상태를 별도로 높인다.

- VIX 급등
- 실제 시장 급락·급등
- 신용스프레드 확대
- 유동성 급감
- 시장 국면의 급격한 전환

5시그마 관측값은 자동으로 데이터 오류로 간주하지 않는다. 실제 시장 충격일 수 있으므로 원천 공급 데이터와 다른 지표로 교차 확인한다.

## 15-4. Drift Monitoring

Drift를 하나의 지표로 처리하지 않는다. 여섯 유형을 따로 감시한다.

| Drift 유형 | 확인할 문제 | 측정 예 | 주의점 |
|---|---|---|---|
| Data Quality Drift | 데이터 수집·품질 문제 | 결측·중복·단위·freshness | 데이터 공급 장애와 시장 상황을 구분 |
| Feature Drift | 입력 데이터 분포 변화 | PSI, KS, Wasserstein 등 | 기준 분포·bin을 학습 구간에서 고정 |
| Prediction Drift | 모델 예측 분포 변화 | 예측값·확률 분포 | 시장 변동과 모델 문제를 구분 |
| Calibration Drift | 예측 확률의 신뢰도 저하 | Brier, reliability curve, ECE | 충분한 라벨·표본 확보 후 평가 |
| Performance Drift | 실제 예측 성능 악화 | OOS MAE, IC, Net Sharpe 등 | 예측 타깃의 실현 시점 이후에만 평가 |
| Target Drift | 예측 타깃(라벨) 분포 변화 | 상승 비율, 수익률 분포 | 기준선(무조건부 상승률, 7-6)도 바뀌므로 같은 창에서 다시 계산해 비교 |

성능 저하가 특정 시장 국면에 몰리는지 국면별로 나눠 본다(7-8).

**다중 시간 창:** 같은 지표를 서로 다른 창에서 감시한다.

- 20거래일: 갑작스러운 단기 변화 감지 (조기 경보 전용)
- 60거래일: 중기 성능·분포 변화 감지
- 120거래일: 장기적인 성능 저하 감지

최근 20거래일 표본만으로 모델을 자동 폐기하지 않는다.

**경보 이후의 행동 단계:**

| 단계 | 시스템 반응 |
|---|---|
| 정상 | 기존 모델 사용 |
| Warning | 데이터와 모델 상태 점검 (Data Quality Drift부터 확인) |
| Critical | 신호 신뢰도 하향 또는 No-Edge(7-11) 전환 검토 |
| 재검증 | 신규 모델 후보를 별도 검증 구간에서 평가 (7-12의 사전 등록 절차) |
| 승인 | 검증을 통과한 모델만 Model Registry에서 교체 |

**임계값:** PSI 0.25, ECE 0.12 같은 고정 수치를 정답으로 가져오지 않는다. 각 모니터의 임계값은 과거 데이터에서 정상 구간의 분포(예: 모델 문제가 없던 기간의 지표 변동)로 정하고, 최소 표본 수, 관측 기간, 경보 지속 조건(예: 연속 몇 개 창에서 초과)을 함께 정의한다. 시장 급변 자체는 Market Risk Control(15-3)의 영역이며 Drift 경보와 구분한다.

---

## 15-5. CI/CD와 배포·롤백

- 모든 푸시와 PR에서 테스트(19장)를 자동 실행하고, 실패하면 main에 합치지 않는다.
- 수집·학습 같은 예약 작업은 테스트 작업과 분리하고, 실패하면 실패로 표시되고 실행 기록(manifest)이 남아야 한다.
- 모델 교체는 Model Registry(7-9)의 승인 상태를 거친다. 이전 승인 모델을 지우지 않고, 롤백은 이전 승인 버전으로 되돌린 뒤 이유를 기록하는 절차로 한다.
- 데이터 스키마·계약을 바꾸는 변경은 새 버전으로 추가하고, 이전 버전을 읽을 수 있는 동안에만 옛 버전을 정리한다.

# 16. 권장 디렉터리 구조

```text
Quant_Finance_v3/
├── README.md
├── MODELS.md
├── DATA_SOURCES.md
├── EXPERIMENTS.md
├── CHANGELOG.md
├── pyproject.toml
├── lockfile (예: uv.lock 또는 poetry.lock; 실제 선택 도구 하나만 유지)
├── requirements-dev.txt
├── .env.example
├── .gitignore
│
├── core/
│   ├── data_contracts.py
│   ├── coverage.py                  # 엔진별 데이터 커버리지 상태 (3-6)
│   ├── pit_schema.py
│   ├── calendar_align.py
│   ├── security_master.py           # 종목코드·ISIN·DART 고유번호의 시점별 매핑 (4-8)
│   ├── sector_mapping.py
│   ├── market_rules.py
│   ├── circuit_breaker.py
│   ├── compliance.py
│   ├── repro.py
│   └── config.py
│
├── collectors/
│   ├── base_client.py
│   ├── kr_market.py
│   ├── kr_fundamentals.py
│   ├── kr_flow_macro.py
│   ├── us_market.py
│   ├── us_macro_sources.py
│   ├── global_sector.py
│   └── alt_news_events.py
│
├── storage/
│   ├── raw_store.py
│   ├── normalized_store.py
│   ├── pit_store.py
│   ├── feature_store.py
│   └── manifests.py
│
├── feature_store/
│   ├── normalizer.py
│   ├── corporate_actions.py
│   ├── global_features.py
│   ├── segment_exposure.py          # 사업부문별 PIT 노출도와 부문별 신뢰도 (5-3)
│   ├── fundamental_features.py
│   ├── factors_kr.py                # PIT 데이터로 직접 만드는 한국 요인 (7-8)
│   ├── disclosure_events.py         # 공시 이벤트와 이벤트 분석 (5-6)
│   ├── regime_features.py
│   └── store_manager.py
│
├── engines/
│   ├── regime_engine.py
│   ├── prediction_engine.py
│   ├── quant_risk_engine.py
│   ├── portfolio_sizing_engine.py
│   ├── fair_value_engine.py
│   ├── financial_health_engine.py
│   ├── investment_attractiveness_engine.py
│   ├── market_risk_engine.py
│   ├── signal_synthesizer.py
│   └── screener_engine.py
│
├── evaluation/
│   ├── experiment_registry.py
│   ├── strategy_lab.py              # 사용자 실험 자동 기록 (7-12)
│   ├── oos_vault.py                 # 잠긴 최종 OOS 구간 접근 통제 (7-12)
│   ├── ablation_runner.py
│   ├── strategy_backtester.py
│   ├── forecast_audit.py
│   ├── metrics.py
│   ├── statistical_tests.py
│   ├── model_registry.py
│   └── results/
│
├── assistant/
│   ├── tools.py
│   ├── rag_indexer.py
│   ├── retrieval.py
│   ├── output_validator.py
│   ├── daily_briefing.py
│   ├── agent_chain.py
│   └── prompts.py
│
├── monitoring/
│   ├── data_quality.py
│   └── drift_monitor.py             # 5개 유형 × 다중 시간 창, 행동 단계 (15-4)
│
├── orchestration/
│   ├── data_ingestion.py
│   ├── feature_pipeline.py
│   ├── forecast_pipeline.py
│   ├── validation_pipeline.py
│   └── briefing_job.py
│
├── api/                         # 확장 단계에서 활성화
│   ├── main.py
│   ├── dependencies.py
│   ├── schemas/
│   └── routers/
│
├── app/
│   ├── app.py
│   ├── views/
│   │   ├── global_radar_briefing.py
│   │   ├── stock_intelligence.py
│   │   ├── screener_factor_lab.py
│   │   ├── portfolio_risk_view.py
│   │   ├── model_validation.py
│   │   └── grounded_ai_chat.py
│   └── components/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── property/
    ├── regression/
    ├── contracts/
    ├── security/
    └── fixtures/
```

초기에는 모든 폴더를 한 번에 생성할 필요가 없다. 해당 Sprint에서 필요한 모듈을 추가하되 계층 간 책임은 유지한다.

---

# 17. 모듈별 입출력 계약

| 모듈 | 입력 | 출력 | Hard Bans |
|---|---|---|---|
| `core/data_contracts.py` | 공급자·시리즈·라이선스 정보 | 사용 목적별 승인 상태 | 미검토 데이터 자동 승인 금지 |
| `core/coverage.py` | 엔진 요구 조건, as-of 시점의 이용 가능 데이터 | 엔진별 `SUFFICIENT/LIMITED/INSUFFICIENT/STALE`와 근거 | 모든 종목에 단일 최소 이력 강제, 결측을 채워 상태 상향 금지 |
| `core/calendar_align.py` | venue calendar, series, availability policy | 시리즈별 PIT 정렬 결과 | 단순 날짜 병합, 임의 08:30 스냅샷, 그룹 없는 merge_asof 금지 |
| `core/pit_schema.py` | 원본 관측·발표·빈티지·수집 정보 | 버전 보존 PIT 레코드 | 과거 revision 덮어쓰기 금지 |
| `collectors/us_macro_sources.py` | 권한 승인된 원천 API/데이터 | 원본 또는 정규화 거시데이터 | FRED 약관 검토 없이 ML 학습·저장·캐싱 금지 |
| `feature_store/global_features.py` | PIT 한국·미국 데이터 | as-of 글로벌 피처 | 미래 구간을 이용한 롤링 계산 금지 |
| `feature_store/segment_exposure.py` | PIT 사업부문 비중·지역 노출, 부문별 드라이버 | 부문별 노출도와 신뢰도, 기업 합산 | 사업부문 비중 고정 딕셔너리·미래 공시 비중 소급 금지 |
| `evaluation/oos_vault.py` | 사전 등록된 후보, Vault 구간 | 1회성 최종 평가와 접근 기록 | 탐색 중 Vault 조회·반복 튜닝 금지 |
| `monitoring/drift_monitor.py` | 기준 분포, 최근 예측·실현값 | 유형·창별 경보와 행동 단계 | 단기 창 하나로 모델 자동 폐기, 근거 없는 고정 임계값 금지 |
| `engines/regime_engine.py` | 과거까지의 피처 | 다축 Regime 상태·확률 | 과거 예측에 Smoothed Probability 사용 금지 |
| `engines/prediction_engine.py` | PIT 피처·폴드 설정 | 타깃별 OOS 예측 | 전체 표본 fit·누출 전처리 금지 |
| `engines/quant_risk_engine.py` | 수익률·분포 가정 | Prediction Interval, VaR/ES | 기간·부호·신뢰수준 누락 금지 |
| `engines/portfolio_sizing_engine.py` | 기본 전략 비중·공분산·제약 | Vol Target/Risk Budget/Risk Parity 결과 | 역변동성 가중치를 Risk Parity라고 부르지 않음 |
| `engines/fair_value_engine.py` | PIT 재무·가정·피어 | Valuation Band와 시나리오 | 부적합 모델 강제 적용·단일 적정가 단정 금지 |
| `engines/financial_health_engine.py` | PIT 재무·업종 | Corporate Financial Health | 금융업에 제조업 Z-score 무차별 적용 금지 |
| `assistant/output_validator.py` | LLM 초안·Tool 결과·증거 | 검증된 주장 또는 거절 | 숫자만 일치하는 것을 의미 정확성으로 간주 금지 |
| `evaluation/strategy_backtester.py` | 신호·가격·비용·시장규칙 | 체결·현금원장·성과 | OHLC만으로 체결 큐를 안다고 가정 금지 |

---

# 18. 개발 로드맵 및 단계별 완료 기준

일정은 개인 개발자의 가용시간, API 승인·권한 확보, 데이터 공급 여부에 따라 달라진다. 날짜를 맞추기 위해 검증을 생략하지 않는다. Sprint는 기간이 아니라 완료 게이트로 관리한다.

## Sprint 0. 저장소 기준선 및 권한 조사

**목표:** 기존 자산을 실제 실행해 기준선을 고정하고 데이터 이용 가능성을 확인한다.

- [ ] 두 GitHub 저장소의 현재 브랜치·커밋·디렉터리 확인
- [ ] 기존 테스트 전체 실행 및 실패 목록 기록
- [ ] 주요 엔진의 입력·출력·부작용 문서화
- [ ] 기존 성능 스냅샷 재현
- [ ] KRX, OpenDART, ECOS, 미국 원천기관, 시장 데이터 공급원별 약관표 작성
- [ ] FRED/ALFRED API는 ML·저장 사용권한 확인 전 수집·학습 파이프라인에 연결하지 않음
- [ ] 검토 전·차단 상태이거나 그 목적의 권한 필드가 없는 소스는 차단되는 테스트 작성

**Gate:** 기존 동작과 한계가 기록되고, 사용 권한이 확인된 최소 데이터 세트가 확정되어야 Sprint 1로 이동한다.

## Sprint 1. PIT 데이터 인프라 및 시간 정렬

- [ ] Data Contract 및 사용 권한 게이트
- [ ] 엔진별 데이터 커버리지 상태 (`SUFFICIENT/LIMITED/INSUFFICIENT/STALE`)
- [ ] Raw/Normalized/PIT 스키마
- [ ] 사업부문 비중의 PIT 레코드와 as-of 조회 (공시 가용 시각 기준)
- [ ] XKRX·미국 시장 venue별 거래 캘린더
- [ ] 조기폐장 및 휴장일 테스트
- [ ] 시리즈별 그룹 정렬 및 `available_at <= decision_time` 불변조건
- [ ] DART 다음 KRX 거래 세션 컷오프
- [ ] 거시지표 발표일시와 수정 이력 분리
- [ ] 한국 연휴 중 미국 누적수익률 집계
- [ ] 수정주가·비수정주가·기업행위 PIT 관리

**Gate:** 승인된 최소 데이터 세트에 대해 재현 가능한 PIT Feature Snapshot을 생성하고, 시간 역전·revision 누출 테스트가 통과한다.

## Sprint 2. Global Features 및 Model A/B/C 검증

- [ ] 타깃·예측 기간·체결 시점 사전 정의
- [ ] 동적 유니버스 및 survivorship 관리
- [ ] Model A/B/C와 단순 기준선 구현
- [ ] Purged/Embargo Walk-forward Split
- [ ] 전처리·피처 선택·튜닝의 fold 내부 fit
- [ ] 통계 지표 및 투자 성과 계산
- [ ] DM/Clark-West/Bootstrap/다중검정 등 적용 가능한 검정
- [ ] Experiment Registry 자동 기록
- [ ] 최종 Holdout 보호 (Final OOS Vault 접근 통제)
- [ ] 업종 단위(Model C-industry)와 사업부문 단위(Model C-segment) 노출도 비교
- [ ] Strategy Lab 실험 자동 기록과 유효 시행 수 기반 다중검정

**Gate:** Model C가 이기든 지든 결과가 동일 조건·동일 표본에서 재현되고, 추가 글로벌 데이터의 기여 또는 무기여가 보고서로 설명된다.

## Sprint 3. Risk, Backtester, Portfolio Sizing

- [ ] GARCH Prediction Interval 및 VaR/ES 명세
- [ ] Kupiec/Christoffersen 진단
- [ ] 한국 시장 가격제한·호가단위·거래정지 제약
- [ ] 주문 참여율·시장충격·수수료·세금
- [ ] 미체결·부분체결·현금 원장
- [ ] Volatility Targeting
- [ ] ES Risk Budget
- [ ] 공분산 기반 Risk Contribution / Risk Parity

**Gate:** 단일 종목 및 소규모 포트폴리오에 대해 비용·체결 가정과 위험 계산의 단위 테스트·통합 테스트가 통과한다.

## Sprint 4. Fair Value 및 기업 분석

- [ ] FCFF/FCFE DCF
- [ ] RIM, DDM, PER/PBR/EV-EBITDA 등 적용 가능 모델
- [ ] 기업 유형별 모델 적합성 매핑
- [ ] Bear/Base/Bull 시나리오 및 민감도 (할인율·성장률 민감도 표, 모델 적용·제외 이유)
- [ ] Corporate Financial Health
- [ ] Investment Attractiveness
- [ ] Market Risk Score
- [ ] F/Z/M 적용 가능성·결측·중복 통제
- [ ] Valuation/Score 백테스트

**Gate:** 기업 유형별 테스트 케이스에서 모델 적용·제외 이유가 명확하고, 계산 결과 및 가정이 재현된다.

## Sprint 5. Signal, Screener, Assistant

- [ ] 기간별 Quant Signal 상태
- [ ] No-Edge / Insufficient Data / Data Stale 분리
- [ ] Screener 및 PIT 커스텀 팩터
- [ ] Forecast Audit Trail (데이터 컷오프·진입 가능 여부·시뮬레이션 체결 구분, 정확도와 투자 성과 분리)
- [ ] 공시 이벤트 분석 (5-6: 가용 시각 기준 이벤트, 사전 고정한 창·기준, 군집 표시)
- [ ] 전략 모의 포트폴리오 기록 (7-13: 사전 등록 전략, 앞으로의 기록만, 투자 권유 아님 표시)
- [ ] 읽기 전용 Tool Calling
- [ ] DART RAG 및 뉴스 Hybrid Search(권한 승인된 데이터만)
- [ ] Numerical Fact-Check 및 Evidence Claim 검증 (지표·기간 유형·연결/별도 의미 일치)
- [ ] Prompt Injection 테스트
- [ ] Golden Question Set 평가

**Gate:** 데이터 부족·출처 누락·수치 불일치 질문에서 안전하게 거절하고, 정상 질문에서 주장과 근거가 추적된다.

## Sprint 6. UI, 운영, 보안 및 출시 검토

- [ ] Streamlit 기반 MVP UI
- [ ] Global Radar / Stock Intelligence / Model Validation
- [ ] Portfolio Risk / Screener / Grounded AI
- [ ] Data Quality 및 Drift Monitoring (5개 유형 × 20·60·120거래일, 행동 단계)
- [ ] Data Integrity Circuit Breaker와 Market Risk Control 분리
- [ ] API·사용자 인증·권한(필요 시)
- [ ] Secrets Management·백업·복구
- [ ] 데이터 라이선스·법률 검토
- [ ] 전체 통합 테스트 및 운영 Runbook

**Gate:** 데모가 정상 동작할 뿐 아니라, 데이터 장애·권한 차단·모델 드리프트·AI 오류 상황에서 기대된 보호 동작을 수행해야 한다.

## 확장 단계 (MVP 이후)

MVP의 검증 가치를 높이지 않으면서 범위만 키우는 기능은 여기로 미룬다. 각각 착수 전에 규제(15-1)와 개인정보 처리를 다시 검토한다.

- [ ] 투자자 유형별 화면 (장기 가치·단기 매매·퀀트·초보·포트폴리오 관리자)
- [ ] 사용자별 알림과 정기 분석 리포트
- [ ] FastAPI 등 외부 API와 별도 프론트엔드

---

# 19. 테스트 전략 및 Definition of Done

## 19-1. 테스트 분류

| 유형 | 예시 |
|---|---|
| Unit | 가격수익률, DCF, VaR, 비용 계산 |
| Integration | Collector → PIT Store → Feature Store |
| Property | 모든 PIT 레코드에서 available_at ≤ decision_time |
| Contract | API 스키마·단위·타임존·라이선스 필드 |
| Regression | 기존 예측·점수·리포트의 의도치 않은 변경 |
| Backtest Integrity | Purge/Embargo·유니버스·체결 시점 |
| Security | Prompt Injection, 권한 우회, 비밀정보 노출 |
| AI Evaluation | 출처·수치·거절·기준일 정확성 |

테스트는 외부 API에 무조건 의존하지 않도록 fixture와 mock을 활용한다. 실제 API 검증은 별도 smoke test로 분리한다.

## 19-2. 필수 PIT 테스트

- 미국 서머타임 전환 전후
- 미국 조기폐장 및 휴장일
- 한국 설·추석 등 장기 휴장 후 재개장
- 동일 날짜의 여러 `series_id`가 서로 섞이지 않는지
- FRED/ALFRED 빈티지 날짜와 실제 발표시각의 분리(권한 승인된 테스트 fixture만)
- DART 금요일·공휴일 전 접수 시 다음 KRX 세션 적용
- 정정공시 전후 과거 as-of 조회 결과
- timezone-naive 입력 거부 또는 명시적 변환
- 오래된 데이터의 staleness 상태
- corporate action 적용 전후의 수익률 재현
- 환율 방향(KRW/USD ↔ USD/KRW)이 뒤집히지 않는지
- 재무제표 결측·단위 변경(원·천원·백만원) 처리
- 같은 지표 이름을 쓰는 여러 회사의 값이 섞이지 않는지 (4-4)
- 휴장 중 미국 누적수익률에서 빠진 세션을 계산하지 않는지 (4-5)

## 19-3. 필수 모델 검증 테스트

- 타깃 기간 중첩 시 Purge/Embargo
- 스케일러·윈저라이저·직교화의 fold 내부 fit
- Model A/B/C 공통 표본 비교
- 공통 표본과 전체 커버리지 결과 분리
- 단순 기준선 포함
- 최종 Holdout 접근 통제
- 다중검정 및 모델 선택 내역 기록
- 예측 확률 calibration 평가
- 거래비용·미체결·가격제한폭 민감도
- 포트폴리오 공분산 행렬의 비정상 상태(비대칭·양의 준정부호 아님·결측) 처리

## 19-4. 최종 Definition of Done

### 데이터

- [ ] 모든 사용 데이터가 Contract Registry에 있고, 목적별 권한이 승인되었다.
- [ ] FRED/ALFRED 데이터는 현재 약관상 금지되는 ML·저장 용도에 연결되지 않았다(별도 서면 허가가 있으면 근거 보존).
- [ ] 시리즈별 시간 정렬과 `available_at <= decision_time` 테스트가 통과한다.
- [ ] DART 정정 이력과 PIT 버전이 보존된다.
- [ ] 시장 데이터의 세션·조기폐장·공급자 가용 지연이 반영된다.
- [ ] corporate action과 상장폐지 이력이 유니버스·수익률에 반영된다.

### 모델 및 전략

- [ ] Model A/B/C와 단순 기준선이 동일 조건에서 비교된다.
- [ ] Purge/Embargo, fold 내부 전처리, Holdout 보호가 구현되어 있다.
- [ ] 예측력 지표와 비용 차감 투자 성과가 분리되어 보고된다.
- [ ] 통계적 우위가 입증되지 않은 신호는 No-Edge로 분류된다.
- [ ] 포지션 사이징에서 Vol Targeting, ES Budget, Risk Parity가 구분된다.
- [ ] Fair Value Range는 기업 유형별 모델 적합성과 가정을 표시한다.
- [ ] Financial Health, Investment Attractiveness, Market Risk가 분리된다.

### AI 및 제품

- [ ] AI 수치는 검증된 Tool 결과와 지표 의미·단위·통화·기준일까지 일치한다.
- [ ] 공시·뉴스는 PIT 및 사용 권한 검사를 통과한다.
- [ ] 프롬프트 인젝션·권한 우회 테스트가 통과한다.
- [ ] 근거가 부족하면 거절한다.
- [ ] 사용자에게 데이터 기준일·출처·불확실성·검증 기간이 표시된다.

### 운영 및 규제

- [ ] Data Integrity Circuit Breaker와 Market Risk Control이 분리되어 있다.
- [ ] 오류·장애·재시도·데이터 노후 상태가 로그에 남는다.
- [ ] 비밀정보·사용자 데이터가 안전하게 관리된다.
- [ ] 데이터 라이선스와 상용 표시 권한을 확인했다.
- [ ] 서비스 기능·유료 여부·개인화·양방향 AI 상담에 대한 법률 검토를 마쳤다.
- [ ] 면책문구만으로 규제 적합성을 주장하지 않는다.

---

# 20. 참고 공식 문서 및 검토 기준

아래 링크는 설계 검토 시 사용한 공식 자료다. API·약관·법령은 변경될 수 있으므로 **구현 직전과 외부 배포 직전에 다시 확인**한다.

## 데이터 및 이용 조건

- [FRED Legal Terms](https://fred.stlouisfed.org/legal/terms/) — FRED 서비스 일반 약관 및 제한
- [FRED API Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html) — API 추가 조건
- [FRED API Documentation](https://fred.stlouisfed.org/docs/api/fred/) — 시리즈·관측·빈티지 관련 API
- [FRED Vintage Dates API](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html) — 값이 변경되거나 신규 발표된 빈티지 날짜
- [BLS Release Schedule](https://www.bls.gov/schedule/2026/) — 미국 노동통계국 발표 일정 및 발표시각
- [BLS CPI Schedule](https://www.bls.gov/schedule/news_release/cpi.htm) — CPI 발표 일정
- [OpenDART API 개발가이드](https://opendart.fss.or.kr/guide/main.do) — 공시 접수일 등 원천 필드 정의
- [KRX 가격제한폭 제도](https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T5.jsp) — 가격제한폭 및 예외
- [KRX 기준가격·가격제한폭(코스닥)](https://regulation.krx.co.kr/contents/RGL/03/03020201/RGL03020201.jsp) — 기준가격 및 상하한가 산출 방식
- [한국은행 ECOS](https://ecos.bok.or.kr/) — 국내 거시 통계

## 라이브러리

- [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) — 거래소 세션 캘린더. 사용 버전과 세션 결과를 공식 거래소 일정과 검증한다.
- [arch](https://arch.readthedocs.io/) — GARCH 계열 모델
- [scikit-learn](https://scikit-learn.org/) — 전처리 및 머신러닝 파이프라인
- [LightGBM](https://lightgbm.readthedocs.io/) — Gradient Boosting
- [DuckDB](https://duckdb.org/docs/) — 로컬 분석 데이터 엔진
- [Pydantic](https://docs.pydantic.dev/) — 구조화 데이터·출력 검증
- [FastAPI](https://fastapi.tiangolo.com/) — API 서비스 확장
- [Streamlit](https://docs.streamlit.io/) — MVP 사용자 인터페이스

## 법령

- [자본시장과 금융투자업에 관한 법률 — 국가법령정보센터](https://www.law.go.kr/법령/자본시장과금융투자업에관한법률) — 제6조, 제101조 등 관련 조항의 현행 문언 확인

---

# 부록 A. 외부 검토안 중 채택하지 않은 제안

외부 검토안(Gemini 작성 v2.1 변형본)의 제안 중 다음은 그대로 도입하지 않는다.

| 제안 | 판단 | 이유 |
|---|---|---|
| 252거래일을 모든 종목의 필수 조건으로 고정 | 수정 | 분석 엔진마다 필요한 이력 길이가 다름 (3-6) |
| 재무제표 8분기를 모든 기업의 절대 조건으로 고정 | 수정 | 모델별 데이터 요구 수준과 기업 특성이 다름 (3-6) |
| PSI·ECE 등 고정 임계값 | 수정 | 실제 데이터와 표본 규모로 검증해야 함 (15-4) |
| VIX 특정 숫자에서 일괄 디레버리징 | 수정 | 시장 상황과 포트폴리오 위험을 함께 고려해야 함 (9-4, 15-3) |
| 기업별 사업부문 비중을 고정 딕셔너리로 저장 | 제외 | 과거 사업구조를 잘못 재현할 수 있음 (5-3) |
| 프롬프트 인젝션 방어율 100% | 제외 | 고정 평가셋 성과가 모든 공격에 대한 방어를 의미하지 않음 (14-3) |
| 10주 내 전체 시스템 완성 | 제외 | 구현 일정은 개발 여건과 데이터 권한에 따라 달라짐 (18) |
| `PUBLIC_ANALYTICS_TOOL`이면 비규제 서비스로 판단 | 제외 | 실제 기능과 영업 방식의 법률 검토가 필요함 (15-1) |

v2.1을 만든 검토 대화에서 나온 제품 제안 중 다음은 범위에서 빼거나 바꿨다 (0-1 제품 범위 결정).

| 제안 | 판단 | 이유 |
|---|---|---|
| 뉴스 원문 분류·감성 분석·실적 발표 콜 분석 | 제외 | 뉴스·콜 원문은 별도 라이선스가 필요함. 공시 이벤트 분석(5-6)으로 대신함 |
| ProPicks 방식 공개 추천 종목 | 수정 | 유사투자자문업 신고 대상이 될 수 있음. 전략 모의 포트폴리오 기록(7-13)으로 대신함 |
| 투자자 유형별 화면, 사용자 알림·리포트 | 보류 | MVP 이후 확장 단계(18) |
| 미국 옵션 내재변동성·VXN (이전 미국 저장소) | 제외 | 이용 조건이 확인된 공급원이 없음. 한국 주식 분석의 핵심 입력이 아님. 권한 있는 공급원이 생기면 계약 등록 후 재검토 |

**마지막 원칙:** 데이터 접근 가능 여부와 사용 권한, 과거 시점 재현 가능 여부, 모델의 통계적 성능, 실제 투자 실행 가능성은 서로 다른 문제다. 본 프로젝트는 이 네 가지를 별도 기준으로 통과시킨 뒤 하나의 제품으로 연결한다.
