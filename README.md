# Quant_Finance_v3

[![tests](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/tests.yml/badge.svg)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/tests.yml)
[![opendart-daily](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/opendart-daily.yml/badge.svg?branch=main)](https://github.com/Hyeonwoo01/Quant_Finance_v3/actions/workflows/opendart-daily.yml)

Global Quant & AI Investment Intelligence Platform — 한국 주식 분석에 미국·글로벌 산업·거시 데이터를 더해,
그 정보가 실제로 예측·위험·기업가치 판단을 개선하는지 Point-in-Time으로 검증하는 플랫폼입니다 (설계·구현 진행 중).

> **English summary.** A research platform that adds US/global industry and macro data to Korean equity analysis and
> tests, strictly point-in-time, whether that information actually improves forecasts, risk and valuation.
> Sprint 1 (this code) is the data foundation: purpose-based data licensing gates, per-engine data coverage status,
> an append-only point-in-time store with revisions, and exchange-calendar time alignment (KRX/NYSE holidays, DST,
> early closes, DART disclosure availability). A daily GitHub Actions job collects OpenDART filings and financial
> statements into the `data` branch after the source's terms were reviewed and registered as a contract.

분석 흐름: 기업의 사업구조 → 글로벌 산업 환경 → 거시경제 → 주가·위험 예측 → 기업가치 평가 → 포트폴리오 위험 관리 → AI 근거 설명

- 설계 문서: [docs/MASTER_GUIDE.md](docs/MASTER_GUIDE.md) (마스터 개발 가이드 v2.2)
- 이전 버전: [Quant_Finance_Project_KOR](https://github.com/Hyeonwoo01/Quant_Finance_Project_KOR) (한국 전용 v2),
  [Quant_Finance_Project](https://github.com/Hyeonwoo01/Quant_Finance_Project) (미국·글로벌 v1)

## 지금 있는 것 (Sprint 1: 데이터 기초)

| 모듈 | 하는 일 | 가이드 |
|---|---|---|
| [`core/data_contracts.py`](core/data_contracts.py) | 데이터 공급원 계약과 목적별 권한 게이트. 저장·캐싱·보관·ML 학습·LLM 입력·공개 표시·재배포·파생 사용을 따로 확인하고, 검토 전·미등록 데이터는 막는다. FRED/ALFRED는 서면 허가 없이 저장·학습 불가 | 3-1 ~ 3-5 |
| [`core/coverage.py`](core/coverage.py) | 분석 엔진별 필요 데이터 정의와 SUFFICIENT / LIMITED / STALE / INSUFFICIENT 판정. 모든 종목에 하나의 최소 이력을 강제하지 않는다 | 3-6 |
| [`core/pit_schema.py`](core/pit_schema.py) | Point-in-Time 레코드(관측 기간·발표 시각·공급자 가용 시각·수집 시각 분리)와 추가 전용 저장소. 정정은 새 revision으로 덧붙이고, as-of 조회는 그 시점에 알 수 있었던 버전만 돌려준다 | 4-1, 4-2 |
| [`core/calendar_align.py`](core/calendar_align.py) | KRX·NYSE 거래소 캘린더 기반 시간 정렬. DART 공시 가용 시각(접수일 다음 KRX 세션 08:30 KST), 서머타임·조기폐장, 회사·시리즈별 as-of 정렬, 한국 연휴 중 미국 누적 수익률(빠진 세션은 계산하지 않음), 거래 세션 기준 staleness | 4-3 ~ 4-7 |

## 매일 자동 수집 (OpenDART)

GitHub Actions [`opendart-daily`](.github/workflows/opendart-daily.yml)가 매일 06:17 KST(한국 장 시작 전 컷오프 08:30 전)에
상장사 공시 목록과 유가증권시장 상장사의 정기보고서 재무제표를 받아 [`data` 브랜치](https://github.com/Hyeonwoo01/Quant_Finance_v3/tree/data)에 쌓습니다.
약관 검토와 수집 범위는 [DATA_SOURCES.md](DATA_SOURCES.md)에 있습니다.

| 모듈 | 하는 일 | 가이드 |
|---|---|---|
| [`core/source_registry.py`](core/source_registry.py) | 약관을 확인하고 등록한 공급원 계약 (지금은 OpenDART 공시 목록·재무제표) | 3-1, 17 |
| [`collectors/base_client.py`](collectors/base_client.py) | 호출 속도·예산 제한, 429·5xx 재시도(지수 백오프·Retry-After), 인증키 가림, 페이지 완결성·스키마 변화 확인 | 6-3 |
| [`collectors/opendart.py`](collectors/opendart.py) | OpenDART 호출과 PIT 변환. 정정 보고서는 바뀐 계정만 새 버전, 가용 시각은 접수일 규칙과 실제 수집 시각 중 늦은 쪽 | 4-3 |
| [`storage/`](storage/) | 변경 불가 원본(내용 주소·해시 검증), PIT 버전 파일(추가 전용·재수집 중복 없음), 실행별 수집 기록 | 6-1, 6-2 |
| [`orchestration/opendart_daily.py`](orchestration/opendart_daily.py) | 매일 실행: 최근 7일 공시 다시 훑기 → 정기보고서 재무제표 대기열 → 사업부문 XBRL → 수집 기록 | 6-4 |
| [`collectors/opendart_segments.py`](collectors/opendart_segments.py), [`feature_store/segment_exposure.py`](feature_store/segment_exposure.py) | XBRL 영업부문·지역 주석에서 부문별 매출·영업이익과 지역별 매출을 PIT로 추출하고, 부문→산업 규칙([`config/segment_taxonomy.json`](config/segment_taxonomy.json))으로 매출·이익 가중 산업 노출도와 부문별 신뢰도를 계산 | 5-3 |

공시 원문 문서(본문·첨부)는 받지 않습니다. 이용 조건을 따로 검토해야 해서입니다.

## 테스트

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

- `tests/unit/` — 모듈별 규칙 (예: 금요일 접수 공시는 월요일 08:30 KST, 추석 연휴 중 미국 3개 세션 수익률 누적, 정정 전후 as-of 조회)
- `tests/property/` — 고정 시드 무작위 사례로 "쓰인 값은 모두 의사결정 시각 이전에 이용 가능했다"는 불변조건과 단순 구현과의 일치를 확인
- GitHub Actions에서 Python 3.12·3.13으로 실행

## 다음 단계 (가이드 18장 로드맵)

1. Sprint 1 나머지: 수정주가·기업행위 PIT 관리, 종목 식별자 매핑(4-8)
2. 다른 공급원(KRX 시세, 한국은행 ECOS) 약관 검토와 계약 등록 → 수집기 (Sprint 1 완료 조건: 재현 가능한 PIT Feature Snapshot)
3. Sprint 2: Strategy Lab 실험 기록과 잠긴 최종 OOS 구간(7-12), 예측 기록 감사(7-10), 업종 단위 vs 사업부문 단위 노출도 비교

API 키는 `.env.example`을 복사한 `.env`에만 넣고 커밋하지 않습니다.
