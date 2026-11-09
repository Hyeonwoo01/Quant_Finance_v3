# Quant_Finance_v3 — 수집 데이터 (`data` 브랜치)

이 브랜치는 GitHub Actions [`opendart-daily`](https://github.com/Hyeonwoo01/Quant_Finance_v3/blob/main/.github/workflows/opendart-daily.yml)가
매일 06:17 KST에 자동으로 커밋합니다. 코드와 설명은 [main 브랜치](https://github.com/Hyeonwoo01/Quant_Finance_v3)에 있습니다.

**출처: 금융감독원 전자공시시스템(DART), OpenDART API — <https://opendart.fss.or.kr>**

- 공시정보는 공시 제출인이 작성했으며 금융감독원은 정확성·완전성을 보장하지 않습니다 (OpenDART 이용약관 제23조).
  각 레코드의 `source_url`에서 원 공시를 확인할 수 있습니다.
- 이용 조건 검토와 수집 범위: [DATA_SOURCES.md](https://github.com/Hyeonwoo01/Quant_Finance_v3/blob/main/DATA_SOURCES.md)

## 구조

| 경로 | 내용 |
|---|---|
| `raw/<source_id>/objects/` | 변경 불가 원본 응답 (gzip, 파일 이름 = SHA-256). 같은 응답은 한 번만 저장 |
| `raw/<source_id>/index/<날짜>.jsonl` | 받을 때마다 한 줄: 요청 파라미터(인증키 가림)·HTTP 상태·수신 시각·원본 해시 |
| `pit_versions/dart_filings/<접수 연-월>.jsonl` | 공시 목록 Point-in-Time 레코드. 비고 변경은 이전 버전을 가리키는 새 버전 |
| `pit_versions/dart_fs/<고유번호>.jsonl` | 재무제표 계정별 Point-in-Time 레코드. 정정 보고서는 바뀐 계정만 새 버전 |
| `manifests/<source_id>/<날짜>/<실행 ID>.json` | 실행별 수집 기록: 요청·건수·경고·오류·권한 상태·코드 버전 |
| `state/opendart_fnltt.json` | 재무제표 대기열 (API에 아직 안 올라온 보고서)·완료·포기 목록 |

`available_at_utc`가 그 값을 쓸 수 있게 된 시각입니다: 접수일 다음 KRX 거래일 08:30 KST와 실제로 받은 시각 이후 첫 08:30 중 늦은 쪽.
