# Quant_Finance_v3

[![tests](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/tests.yml/badge.svg)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/tests.yml)
[![opendart-daily](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/opendart-daily.yml/badge.svg?branch=main)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/opendart-daily.yml)
[![macro-daily](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/macro-daily.yml/badge.svg?branch=main)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/macro-daily.yml)
[![krx-daily](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/krx-daily.yml/badge.svg?branch=main)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/krx-daily.yml)

**Global Quant & AI Investment Intelligence Platform** — 한국 주식 분석에 미국·글로벌 산업·거시 데이터를 더하고,
그 정보가 실제로 예측·위험·기업가치 판단을 개선하는지 **Point-in-Time(PIT)** 으로 검증하는 연구 플랫폼입니다.
지금은 Sprint 1(데이터 기초)을 구현하고 있습니다.

> **English summary.** A research platform that adds US/global industry and macro data to Korean equity analysis and
> tests, strictly point-in-time, whether that information actually improves forecasts, risk and valuation.
> The current code is the data foundation: purpose-based data licensing gates, an append-only point-in-time store
> with revisions, exchange-calendar time alignment, a point-in-time security master, and daily GitHub Actions jobs
> that collect OpenDART filings/financials/segments and public industry & macro series. Data whose licence forbids
> redistribution (Korean exchange prices) is written only to a separate private repository, enforced in code.
> On top sits a small evidence-grounded agent: the point-in-time store is exposed as a read-only **MCP server**, and a
> function-calling loop (Claude or GPT, behind a small provider adapter) answers questions with an `evidence_id` on every number, which a verifier checks
> against the source rows (value, unit, period, consolidation, metric) and marks VERIFIED / WARNING / ABSTAINED.

| 문서 | 내용 |
|---|---|
| [docs/MASTER_GUIDE.md](docs/MASTER_GUIDE.md) | 마스터 개발 가이드 v2.2 — 설계 기준. 이 README의 `가이드 x-y`는 이 문서의 절 번호 |
| [DATA_SOURCES.md](DATA_SOURCES.md) | 공급원별 약관 원문 검토, 목적별 권한 판단, 수집 범위와 시간 규칙 |
| 이전 버전 | [Quant_Finance_Project_KOR](https://github.com/Hyeonwoo01/Quant_Finance_Project_KOR) (한국 전용 v2), [Quant_Finance_Project](https://github.com/Hyeonwoo01/Quant_Finance_Project) (미국·글로벌 v1) |

## 목차

1. [개요](#1-개요)
2. [현재 상태](#2-현재-상태)
3. [아키텍처](#3-아키텍처)
4. [디렉터리 구조](#4-디렉터리-구조)
5. [데이터 공급원과 권한](#5-데이터-공급원과-권한)
6. [매일 자동 수집](#6-매일-자동-수집)
7. [저장 구조](#7-저장-구조)
8. [AI 어시스턴트 (DART + MCP + Agent)](#8-ai-어시스턴트-dart--mcp--agent)
9. [시작하기](#9-시작하기)
10. [개발 규칙](#10-개발-규칙)
11. [다음 단계](#11-다음-단계)

---

## 1. 개요

분석 흐름 (가이드 1장):

```text
기업의 사업구조 → 글로벌 산업 환경 → 거시경제 → 주가·위험 예측 → 기업가치 평가 → 포트폴리오 위험 관리 → AI 근거 설명
```

핵심 질문은 "글로벌 데이터를 더하면 한국 주식 판단이 실제로 나아지는가"입니다.
그래서 모든 데이터를 **그 시점에 실제로 알 수 있었던 값**으로만 저장·조회합니다.
나중에 받은 값으로 과거를 채우지 않고, 정정은 덮어쓰지 않고 새 버전으로 남깁니다.

## 2. 현재 상태

가이드 18장 로드맵 기준입니다. ✅ 완료 · 🔶 일부 · ⏳ 준비됨(실행 전) · ⬜ 아직

### Sprint 1. PIT 데이터 인프라 및 시간 정렬

| 항목 | 상태 | 구현 |
|---|---|---|
| Data Contract 및 사용 권한 게이트 | ✅ | `core/data_contracts.py`, `core/source_registry.py` |
| 엔진별 데이터 커버리지 상태 | ✅ | `core/coverage.py` |
| Raw / PIT 스키마 | ✅ | `storage/raw_store.py`, `core/pit_schema.py`, `storage/pit_store.py` |
| 사업부문 비중의 PIT 레코드와 as-of 조회 | ✅ | `collectors/opendart_segments.py`, `feature_store/segment_exposure.py` |
| XKRX·NYSE 캘린더, 조기폐장·휴장, DART 컷오프 | ✅ | `core/calendar_align.py` |
| `available_at <= decision_time` 불변조건 | ✅ | `tests/property/test_pit_invariants.py` |
| 한국 연휴 중 미국 누적수익률 | ✅ | `core/calendar_align.py` |
| 종목 식별자 시점별 매핑 (4-8) | ✅ | `core/security_master.py` — 수집 시작(2026-11-11) 이후만 |
| 거시지표 수정 이력 | 🔶 | 값 변경은 새 버전. 공식 발표 시각은 아직 없음(받은 시각 기준) |
| 비수정주가 | ✅ | `collectors/fsc_krx.py` — 2023-11-10 기준일부터 비공개 저장소에 수집 중 (728거래일, 약 208만 건) |
| 수정주가·기업행위 PIT | ⬜ | 배당·권리일정 공급원 검토 필요 |

AI 어시스턴트(가이드 14장, Sprint 5 항목)는 데이터 기초 위에서 먼저 작게 만들었다: 읽기 전용 MCP 서버, Function Calling Agent, 주장별 근거 검증, 평가 세트 ([8절](#8-ai-어시스턴트-dart--mcp--agent)).

**Gate:** 재현 가능한 PIT Feature Snapshot 생성 — 가격 과거 구간과 기업행위가 갖춰진 뒤 판단합니다.

### 실제로 모이고 있는 데이터 (2026-11-10 기준)

| 데이터 | 규모 | 저장 위치 |
|---|---|---|
| 유가·코스닥 공시 목록 | 매일 최근 7일 재확인 | 공개 `data` 브랜치 |
| 유가증권시장 정기보고서 재무제표 (별도·연결) | 보고서가 나오는 대로 | 공개 `data` 브랜치 |
| 사업부문·지역별 매출·이익 (XBRL) | 재무제표를 받은 보고서 | 공개 `data` 브랜치 |
| 종목 식별자 매핑 | 4,013개 회사 | 공개 `data` 브랜치 |
| 관세청 품목별 수출입 (반도체·선박·자동차·축전지) | 월별, 최근 14개월 | 공개 `data` 브랜치 |
| 한국은행 ECOS (기준금리·국고채 3년·수출물가 7종) | 월별·일별 | 공개 `data` 브랜치 |
| 미국 EIA (원유 재고·천연가스 저장량) | 주별 | 공개 `data` 브랜치 |
| 한국 주식 일별 시세 (코스피·코스닥·코넥스 전 종목, 하루 약 2,900개) | 2023-11-10 기준일부터 (최근 3년) | **비공개 저장소** |

## 3. 아키텍처

가이드 2-1의 데이터 흐름 중 지금 구현된 부분입니다.

```text
 공급원 (OpenDART · 관세청 · 한국은행 ECOS · 미국 EIA · 금융위원회 주식시세)
   │
   │  ① 계약 확인 — 등록되지 않았거나 그 목적이 승인되지 않은 데이터는 막는다 (Fail Closed)
   ▼
 core/source_registry.py ──► core/data_contracts.py
   │
   │  ② 수집 — 속도·호출 예산 제한, 재시도, 인증키 가림, 페이지 완결성·스키마 확인
   ▼
 collectors/*  (base_client.py 위에 공급원별 클라이언트)
   │
   │  ③ 저장 — 원본은 해시로, 값은 PIT 버전으로. 저장소가 공개면 재배포 허용 데이터만
   ▼
 storage/raw_store.py      변경 불가 원본 (gzip, SHA-256 주소)
 storage/pit_store.py      PIT 버전 파일 (추가 전용, 정정은 새 revision)
 storage/manifests.py      실행별 수집 기록
 storage/visibility.py     공개/비공개 저장소 가드
   │
   │  ④ 조회 — available_at <= 의사결정 시각인 버전만
   ▼
 core/pit_schema.py (query_as_of) · core/calendar_align.py · core/security_master.py
   │
   ▼
 feature_store/segment_exposure.py   사업부문 → 산업 노출도
   │
   ▼
 (다음: engines/ · evaluation/ — Sprint 2 이후)

 orchestration/*_daily.py  ①~③을 묶어 GitHub Actions에서 매일 실행
```

### 시간 규칙 (가이드 4장)

| 데이터 | 가용 시각 (`available_at_utc`) |
|---|---|
| DART 공시·재무제표·사업부문 | 접수일 다음 KRX 세션 08:30 KST와, 실제로 받은 뒤 첫 08:30 컷오프 중 늦은 쪽 |
| 관세청·ECOS·EIA | 실제로 받은 뒤 첫 08:30 KST 컷오프 (공급원이 발표 시각·초판 값을 주지 않음) |
| 한국 주식 일별 시세 | 공급자가 다음 영업일 13시에 주므로 T+2 세션 08:30 KST (과거 채우기에도 같은 규칙) |
| 종목 식별자 매핑 | 실제로 받은 뒤 첫 08:30 컷오프. 그 전 시점은 "알 수 없음" |

## 4. 디렉터리 구조

가이드 16장의 권장 구조를 따릅니다. 아직 없는 디렉터리(`engines/`, `evaluation/` 등)는 해당 Sprint에서 만듭니다.

```text
Quant_Finance_v3/
├── README.md
├── DATA_SOURCES.md                  # 공급원 약관 검토와 권한 판단
├── docs/MASTER_GUIDE.md             # 설계 기준 문서
├── pyproject.toml · requirements*.txt · .env.example
│
├── core/                            # 규칙과 모델 (외부 호출 없음)
│   ├── data_contracts.py            # 계약·목적별 권한 게이트 (3-1~3-5)
│   ├── source_registry.py           # 등록된 공급원 계약 목록 (3-1, 17)
│   ├── coverage.py                  # 엔진별 데이터 커버리지 상태 (3-6)
│   ├── pit_schema.py                # PIT 레코드·as-of 조회 (4-1, 4-2)
│   ├── calendar_align.py            # 거래소 캘린더·가용 시각·as-of 정렬 (4-3~4-7)
│   ├── security_master.py           # 종목 식별자 시점별 매핑 (4-8)
│   └── repro.py                     # 코드 버전 기록
│
├── collectors/                      # 공급원별 호출과 응답 → PIT 레코드 변환
│   ├── base_client.py               # 속도·예산 제한, 재시도, 인증키 가림 (6-3)
│   ├── opendart.py                  # 공시 목록·재무제표 (4-3)
│   ├── opendart_xbrl.py             # XBRL 해석 (원본 zip은 저장 안 함)
│   ├── opendart_segments.py         # 영업부문·지역 숫자 추출 (5-3)
│   ├── opendart_corpcode.py         # 고유번호 ↔ 종목코드 (4-8)
│   ├── macro_sources.py             # 관세청·ECOS·EIA (5-2)
│   └── fsc_krx.py                   # 금융위원회 주식시세 — 비공개 저장 전용 (8-1)
│
├── storage/                         # 파일 저장 (6-1, 6-2)
│   ├── raw_store.py · pit_store.py · manifests.py
│   └── visibility.py                # 공개 저장소에는 재배포 허용 데이터만
│
├── feature_store/
│   └── segment_exposure.py          # 사업부문 → 산업·지역 노출도 (5-3)
│
├── orchestration/                   # 매일 실행 진입점 (6-4)
│   ├── opendart_daily.py · macro_daily.py · krx_daily.py
│
├── assistant/                       # 근거 기반 AI 어시스턴트 (14장)
│   ├── tools.py                     # 읽기 전용 PIT 조회 (LLM 입력 허용 데이터만, as_of 필터)
│   ├── mcp_server.py                # 위 도구를 MCP 서버로 (stdio)
│   ├── agent.py                     # MCP 클라이언트 + Function Calling 루프 (공급자 무관)
│   ├── llm_backends.py              # Claude·GPT 어댑터 (도구 형식·요청·응답 변환)
│   ├── verify.py                    # 주장별 근거 검증 VERIFIED / WARNING / ABSTAINED (14-2)
│   └── eval/                        # 평가 세트와 실행기 (14-4)
│
├── config/
│   ├── segment_taxonomy.json        # 부문 레이블 → 산업 규칙
│   └── macro_series.json            # 받을 품목·통계·시리즈 목록
│
├── tests/
│   ├── unit/                        # 모듈별 규칙
│   ├── integration/                 # 가짜 OpenDART로 여러 날 실행
│   ├── property/                    # 고정 시드 무작위 사례로 PIT 불변조건
│   └── fake_opendart.py · conftest.py · fixtures/
│
└── .github/workflows/               # tests · opendart-daily · macro-daily · krx-daily · assistant-eval
```

## 5. 데이터 공급원과 권한

수집기를 만들기 전에 약관을 원문으로 확인하고 계약으로 등록합니다. 판단 근거는 [DATA_SOURCES.md](DATA_SOURCES.md)에 있습니다.

| 공급원 | 계약 ID | 저장 | ML 학습 | 공개 표시 | 원본 재배포 | 저장 위치 |
|---|---|---|---|---|---|---|
| OpenDART 공시 목록·재무제표·고유번호 | `opendart_list_v1` · `opendart_fnltt_v1` · `opendart_corpcode_v1` | ✅ | ✅ | ✅ | ✅ | 공개 |
| OpenDART XBRL (숫자만 추출) | `opendart_xbrl_v1` | ✅ | ✅ | ✅ | ❌ | 공개 (원본 zip 없음) |
| 관세청 품목별 수출입 | `kcs_itemtrade_v1` | ✅ | ✅ | ✅ | ✅ | 공개 |
| 한국은행 ECOS (한국은행 작성 통계만) | `bok_ecos_v1` | ✅ | ✅ | ✅ | ✅ | 공개 |
| 미국 EIA (EIA 작성 시리즈만) | `eia_v1` | ✅ | ✅ | ✅ | ✅ | 공개 |
| 금융위원회 주식시세 (원천: 한국거래소) | `fsc_krx_stock_price_v1` | ✅ | ❌ (문의 중) | ❌ | ❌ | **비공개만** |

FRED/ALFRED는 서면 허가 없이 저장·학습에 쓰지 않습니다 (가이드 3-2). 계약이 바뀌면 기존 계약을 고치지 않고 새 ID로 등록합니다.

## 6. 매일 자동 수집

| 워크플로 | 시각 (KST) | 하는 일 | 저장 | 수동 실행 모드 |
|---|---|---|---|---|
| [`opendart-daily`](.github/workflows/opendart-daily.yml) | 매일 06:17 | 종목 식별자 매핑 → 최근 7일 공시 → 정기보고서 재무제표 → 사업부문 XBRL | 공개 `data` 브랜치 | `collect` · `check` · `probe-xbrl` |
| [`macro-daily`](.github/workflows/macro-daily.yml) | 매일 07:47 | 관세청·ECOS·EIA 지표 | 공개 `data` 브랜치 | `collect` · `check` · `probe` |
| [`krx-daily`](.github/workflows/krx-daily.yml) | 평일 14:23 | 한국 주식 일별 시세 (전 종목) | **비공개 저장소** | `collect` · `backfill` · `check` · `probe` |
| [`assistant-eval`](.github/workflows/assistant-eval.yml) | 수동 | AI 어시스턴트 평가 세트를 실제 모델로 실행 | 실행 요약·아티팩트 | — |
| [`tests`](.github/workflows/tests.yml) | push·PR | `pytest` (Python 3.12·3.13) | — | — |

- `check`·`probe`는 저장하지 않고 인증키·연결·응답 구조만 확인합니다.
- 한 공급원이 실패해도 다른 공급원은 계속합니다. 실패하면 받은 만큼과 수집 기록은 남기고 워크플로를 실패로 표시합니다.
- `data` 브랜치에 쓰는 두 워크플로는 같은 concurrency 그룹이라 동시에 돌지 않습니다.
- Actions 로그는 공개입니다. 인증키는 Secrets로만 넘기고, 비공개 데이터의 값은 로그에 출력하지 않습니다.

## 7. 저장 구조

[`data` 브랜치](https://github.com/Hyeonwoo01/Quant_Finance_v3/tree/data)와 비공개 저장소는 같은 배치를 씁니다.

```text
raw/<source_id>/objects/<sha[:2]>/<sha>.gz       원본 응답 (같은 내용은 한 번만, 인증키 가림)
raw/<source_id>/index/<YYYY-MM-DD>.jsonl         받을 때마다 한 줄 (주소·파라미터·상태·해시)
pit_versions/<series_id>/<partition>.jsonl       PIT 레코드 (추가 전용, 원본이 정정본보다 앞)
manifests/<source_id>/<YYYY-MM-DD>/<run_id>.json 실행별 수집 기록 (건수·경고·오류·권한 상태)
state/                                           대기열 (재무제표·XBRL 처리 상태)
PRIVATE_STORE                                    비공개 저장소 표식 (비공개 저장소에만)
```

주요 시리즈 (`series_id`): `dart_filings` · `dart_fs` · `dart_segments` · `dart_corp_master` · `kcs_itemtrade` · `bok_ecos` · `eia` · `krx_daily_price`(비공개)

저장된 데이터를 읽는 예:

```python
from datetime import datetime, timezone
from core.data_contracts import Purpose
from core.source_registry import default_registry
from storage.pit_store import PITFileStore

store = PITFileStore("data", default_registry()).load(["bok_ecos"])
df = store.query_as_of(datetime(2026, 11, 12, tzinfo=timezone.utc), Purpose.ML_TRAINING)   # 그 시점에 알 수 있었던 값만
```

## 8. AI 어시스턴트 (DART + MCP + Agent)

수집한 PIT 데이터를 **MCP 서버**로 감싸고, 모델이 **Function Calling**으로 그 도구를 골라 부르는 작은 Agent입니다.
모델은 **Claude**(Anthropic)와 **GPT**(OpenAI) 중에서 고릅니다. 도구·기준 시각 강제·근거 검증은 공급자와 무관하게 같아서, 같은 평가 세트로 두 모델을 비교할 수 있습니다.
가이드 14장(Evidence-Grounded AI Assistant)을 따릅니다. 목표는 "환각 0"이 아니라 **근거를 달고, 근거와 다른 숫자는 잡아내는 것**입니다.

```text
질문 + 기준 시각(as_of, 사람이 정함)
  │
  ▼
assistant/agent.py ── Claude 또는 GPT (Function Calling) ─────────────┐
  │  assistant/llm_backends.py: 공급자별 도구 형식·요청·응답 변환        │
  │  MCP 클라이언트 (stdio)                                              │ 도구 선택·호출 반복
  ▼                                                                      │
assistant/mcp_server.py  ◄───────────────────────────────────────────────┘
  │  data_permissions · find_company · search_dart_filings · get_financials · list_macro_series · get_macro_series
  ▼
assistant/tools.py → PIT 저장소 (data 브랜치)    query_as_of(as_of, Purpose.LLM_INPUT)
  │
  ▼
submit_answer (답 + 주장별 evidence_id) → assistant/verify.py → VERIFIED / WARNING / ABSTAINED + 출처
```

| 지키는 것 | 방법 |
|---|---|
| 이용 조건 | 계약이 **LLM 입력**을 허용한 데이터만 도구가 돌려준다. 한국 주식 시세(재배포 금지)와 XBRL 부문 숫자는 어떤 도구로도 나가지 않는다 |
| 미래 정보 차단 | 기준 시각은 사람이 정하고 Agent가 모든 도구 호출에 강제로 넣는다. 모델이 다른 시각을 넣으면 덮어쓰고 기록한다. 도구는 그 시각에 이용 가능했던 버전만 본다 |
| 근거 연결 | 도구가 돌려주는 모든 값에 `evidence_id`(PIT revision)·출처 URL·가용 시각·계약 ID가 붙는다. 답의 숫자마다 근거를 달아 제출한다 |
| 숫자 검증 | 값(억원·조원 등 표시 단위 환산)·단위·기간·분기/누적·연결/별도·지표 이름까지 근거 행과 대조한다. 근거 없는 숫자도 찾아낸다 |
| 권한 최소화 | 도구는 읽기 전용(MCP `readOnlyHint`)이고 정해진 파라미터만 받는다. 코드 실행·웹·쓰기 도구는 없다 |
| 프롬프트 인젝션 | 공시 제목 같은 외부 텍스트는 데이터로만 다루라고 지시하고(`untrusted_text` 표시), 그 텍스트를 따라 만든 숫자는 검증에서 걸린다 |
| 감사 기록 | 단계마다 도구 이름·입력·오류·기준 시각 덮어쓰기·토큰을 남긴다 |

실행:

```bash
pip install -r requirements.txt -r requirements-assistant.txt
git clone -b data --depth 1 https://github.com/Hyeonwoo01/Quant_Finance_v3.git data     # 공개 PIT 데이터

# Agent — Claude (ANTHROPIC_API_KEY 필요, 기본 모델 claude-opus-5-5)
python -m assistant.agent --data-dir data --as-of 2026-11-12T08:30:00+09:00 "에스엘의 최근 연결 영업이익은?"

# Agent — GPT (OPENAI_API_KEY 필요, 기본 모델 gpt-5.5 · --model 또는 OPENAI_MODEL로 변경)
python -m assistant.agent --provider openai --data-dir data --as-of 2026-11-12T08:30:00+09:00 "에스엘의 최근 연결 영업이익은?"

# MCP 서버만 (Claude Desktop·Claude Code 같은 MCP 클라이언트에서 쓰기)
QF_DATA_DIR=data python -m assistant.mcp_server
```

Claude Code에 붙이려면 저장소 루트에서: `claude mcp add --transport stdio --env QF_DATA_DIR=$PWD/data quant-pit-data -- python -m assistant.mcp_server`

평가 (가이드 14-4): [`assistant/eval/golden.jsonl`](assistant/eval/golden.jsonl)의 질문 7개를 실제 모델로 돌려 답·보류 판단, 도구 선택, 근거 연결, 주장 VERIFIED 비율, 근거 없는 숫자 수를 잽니다.
보류해야 맞는 질문(시세처럼 이용 조건상 막힌 데이터, 수집 시작 전 시점, 미래)도 들어 있습니다.
질문마다 기준 시각이 고정돼 있어 데이터가 계속 쌓여도 결과가 재현됩니다. GitHub Actions [`assistant-eval`](.github/workflows/assistant-eval.yml)을 수동으로 실행하고, 공급자(anthropic·openai)를 고를 수 있습니다(API 비용 발생).

## 9. 시작하기

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

로컬에서 수집을 돌려 보려면 `.env.example`을 `.env`로 복사해 키를 넣고(커밋하지 않음) 환경변수로 불러옵니다.

```bash
python -m orchestration.opendart_daily --check                 # 저장 없이 키·연결 확인
python -m orchestration.macro_daily --probe                    # 저장 없이 응답 구조 확인
python -m orchestration.opendart_daily --data-dir data         # 실제 수집 (data/ 아래에 저장)
```

GitHub Actions 설정 (Settings → Secrets and variables → Actions):

| 이름 | 종류 | 쓰는 곳 |
|---|---|---|
| `DART_API_KEY` | Secret | opendart-daily |
| `DATA_GO_KR_KEY` | Secret | macro-daily(관세청), krx-daily — 공공데이터포털 인증키. API마다 활용신청 필요 |
| `ECOS_API_KEY` · `EIA_API_KEY` | Secret | macro-daily |
| `ANTHROPIC_API_KEY` · `OPENAI_API_KEY` | Secret | assistant-eval (수동 실행, 고른 공급자의 키만 필요, API 비용 발생) |
| `PRIVATE_DATA_TOKEN` | Secret | krx-daily — 비공개 저장소 Contents 읽기·쓰기 토큰 |
| `PRIVATE_DATA_REPO` | Variable (선택) | krx-daily — 비공개 저장소 이름. 없으면 `<소유자>/Quant_Finance_v3_private` |

## 10. 개발 규칙

가이드 0-3의 원칙 가운데 코드에서 강제하는 것들입니다.

- **권한 먼저:** 새 공급원은 약관 검토 → 계약 등록 → 수집기 순서. 권한이 확인되지 않으면 막는다 (`ContractRegistry.require`).
- **PIT:** `available_at_utc`는 관측일을 복사해 만들지 않는다. 지금 값으로 과거를 채우지 않는다. 정정은 새 revision.
- **재배포 금지 데이터는 공개 저장소에 쓰지 않는다:** `storage/visibility.py`가 쓰기 전에 막는다.
- **비밀 값:** 인증키는 환경변수로만 받고 로그·오류·저장 기록에 남기지 않는다 (`collectors/base_client.py`).
- **실패는 드러낸다:** 마지막 값을 최신처럼 쓰지 않는다. 페이지가 모자라거나 파일이 잘렸으면 저장하지 않고 오류로 남긴다.
- **테스트:** 새 규칙에는 테스트를 붙인다. 네트워크 없이 가짜 전송으로 돌린다.

## 11. 다음 단계

1. **저장 형식 Parquet 전환:** 비공개 저장소가 1GB를 넘거나 2023년 이전을 채우기 전에 한다 (가이드 6-1 구현 노트).
   최근 3년 시세는 채웠다 (2023-11-10 ~).
   **예약 실행 지연:** GitHub 예약 실행이 5~8시간 늦게 돈다. 실전 08:30 판단에 쓰기 전에 정시 실행 수단을 정한다.
2. **Sprint 1 마무리:** 수정주가·기업행위(배당·권리일정) 공급원 검토, 첫 PIT Feature Snapshot.
3. **사업부문 분류 규칙 v0.2:** 3분기 보고서(11/10~16)가 쌓인 뒤 미분류 레이블 반영.
4. **Sprint 2:** 타깃 사전 정의, Purged Walk-forward, Model A/B/C 비교, 실험 기록과 잠긴 최종 OOS 구간 (가이드 7장).
