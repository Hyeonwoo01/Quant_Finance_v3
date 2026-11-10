"""OpenDART 고유번호 파일(corpCode.xml) → 상장사의 고유번호·종목코드 매핑을 PIT 레코드로 (가이드 4-8).

corpCode.xml은 DART에 등록된 모든 회사(약 11만 개)의 지금 상태만 준다. 과거 이력은 주지 않는다. 그래서
- 종목코드가 있는 회사만 남기고, 회사마다 {회사명, 종목코드}가 바뀔 때만 새 버전을 덧붙인다.
- 가용 시각은 실제로 받은 시각 이후 첫 08:30 KST 컷오프다. 수집을 시작하기 전의 매핑은 알 수 없다
  (지금 종목코드로 과거를 채우지 않는다 — core/security_master.py가 BEFORE_COVERAGE로 답한다).
- 전에 종목코드가 있던 회사가 파일에서 종목코드를 잃거나 사라지면 종목코드가 빈 새 버전을 남긴다.
원본 zip은 저장하지 않는다. 대부분이 비상장사 행이고 매일 새로 만들어지는 파일이라 저장소만 키운다.
대신 수집 기록에 zip·XML의 SHA-256을 남기고, 상장사 행의 내용은 PIT 레코드에 모두 들어 있다.
"""
import hashlib
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Optional

from collectors.base_client import CollectorError, HttpResponse
from collectors.opendart import PROVIDER, OpenDartClient, OpenDartError
from core.calendar_align import first_kr_cutoff_at_or_after
from core.pit_schema import PITRecord

SERIES_CORP_MASTER = "dart_corp_master"
METRIC_IDENTITY = "identity"
ENDPOINT = "corpCode.xml"
SOURCE_URL = "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018"
MIN_LISTED = 1000            # 상장사 행이 이보다 적으면 파일이 잘렸다고 보고 아무것도 바꾸지 않는다
MAX_DROP_RATIO = 0.2         # 한 번에 이 비율 넘게 종목코드가 사라지면 파일 이상으로 보고 멈춘다


class CorpCodeError(CollectorError):
    """고유번호 파일이 잘렸거나 형식이 바뀜 — 매핑을 바꾸지 않고 멈춘다."""


@dataclass(frozen=True)
class CorpCodeFile:
    response: HttpResponse
    zip_sha256: str
    xml_sha256: str
    rows: list            # {"corp_code", "corp_name", "stock_code", "modify_date"} (모든 회사)


def parse_corp_codes(xml: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise CorpCodeError(f"corpCode.xml 해석 실패: {e}") from None
    rows = []
    for el in root.iter("list"):
        code = (el.findtext("corp_code") or "").strip()
        if not code:
            raise CorpCodeError("corp_code가 빈 행이 있음")
        rows.append({"corp_code": code, "corp_name": (el.findtext("corp_name") or "").strip(),
                     "stock_code": (el.findtext("stock_code") or "").strip(),
                     "modify_date": (el.findtext("modify_date") or "").strip()})
    if len({r["corp_code"] for r in rows}) != len(rows):
        raise CorpCodeError("같은 corp_code가 두 번 나옴")
    return rows


def fetch_corp_codes(client: OpenDartClient) -> CorpCodeFile:
    """고유번호 zip을 받아 풀어 해석한다. 오류 응답(XML·JSON)은 OpenDartError."""
    resp = client.http.get(ENDPOINT, {"crtfc_key": client._key})
    body = resp.body
    if body[:2] != b"PK":
        status, message = "parse", "zip이 아닌 응답"
        try:
            root = ET.fromstring(body)
            status, message = root.findtext("status") or status, root.findtext("message") or message
        except ET.ParseError:
            try:
                d = json.loads(body.decode("utf-8"))
                status, message = str(d.get("status", status)), str(d.get("message", message))
            except (ValueError, UnicodeDecodeError):
                pass
        raise OpenDartError(status, message, ENDPOINT)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if len(names) != 1:
                raise CorpCodeError(f"zip 안의 XML이 {len(names)}개")
            xml = z.read(names[0])
    except zipfile.BadZipFile as e:
        raise CorpCodeError(f"zip 손상: {e}") from None
    return CorpCodeFile(resp, hashlib.sha256(body).hexdigest(), hashlib.sha256(xml).hexdigest(), parse_corp_codes(xml))


def _content(name: str, stock_code: str) -> str:
    return json.dumps({"corp_name": name, "stock_code": stock_code}, ensure_ascii=False, sort_keys=True)


def master_records(rows: list[dict], previous: Iterable[PITRecord], *, contract_id: str, raw_sha: str,
                   ingested_at_utc: datetime) -> tuple[list[PITRecord], dict]:
    """고유번호 파일의 행과 저장된 최신 버전을 비교해 바뀐 회사만 새 버전으로 만든다.

    previous: 저장된 dart_corp_master 레코드 전부 (순서 무관). 반환: (새 레코드, 건수).
    건수 키: with_code(지금 종목코드가 있는 회사), new(처음 보는 회사), changed(회사명·종목코드 변경),
    code_removed(종목코드가 비거나 파일에서 사라짐), unchanged.
    """
    latest: dict[str, PITRecord] = {}
    for r in previous:
        cur = latest.get(r.entity_id)
        if cur is None or (r.available_at_utc, r.ingested_at_utc) >= (cur.available_at_utc, cur.ingested_at_utc):
            latest[r.entity_id] = r
    by_code = {r["corp_code"]: r for r in rows}
    listed = {c: r for c, r in by_code.items() if r["stock_code"]}
    had_code = {e for e, r in latest.items() if json.loads(r.value)["stock_code"]}
    if len(listed) < MIN_LISTED:
        raise CorpCodeError(f"종목코드가 있는 회사가 {len(listed)}개뿐 — 파일이 잘린 것으로 보고 매핑을 바꾸지 않음")
    gone = had_code - set(listed)
    if had_code and len(gone) > MAX_DROP_RATIO * len(had_code):
        raise CorpCodeError(f"종목코드가 있던 회사 {len(had_code)}개 중 {len(gone)}개가 한 번에 사라짐 — 파일 이상으로 보고 멈춤")

    avail = first_kr_cutoff_at_or_after(ingested_at_utc).to_pydatetime()
    counts = {"with_code": len(listed), "new": 0, "changed": 0, "code_removed": 0, "unchanged": 0}
    out = []

    def record(corp_code: str, name: str, stock_code: str, attrs: dict, kind: str) -> None:
        value = _content(name, stock_code)
        prev = latest.get(corp_code)
        if prev is not None and prev.value == value:
            counts["unchanged"] += 1
            return
        rev = f"{corp_code}#{hashlib.sha256(value.encode()).hexdigest()[:12]}"
        rec = PITRecord(entity_id=corp_code, series_id=SERIES_CORP_MASTER, metric_id=METRIC_IDENTITY,
                        available_at_utc=avail, ingested_at_utc=ingested_at_utc, revision_id=rev, value=value,
                        unit="json", source_provider=PROVIDER, raw_payload_hash=raw_sha,
                        license_contract_id=contract_id, vintage_date=ingested_at_utc.date(), source_url=SOURCE_URL,
                        attributes=json.dumps(attrs, ensure_ascii=False, sort_keys=True))
        if prev is not None:      # 예전 값으로 되돌아가도(A→B→A) 새 버전으로 남도록 받은 시각을 붙인다
            rec = replace(rec, revision_id=f"{rev}@{ingested_at_utc:%Y%m%dT%H%M%S}",
                          supersedes_revision_id=prev.revision_id)
        counts["new" if prev is None else kind] += 1
        out.append(rec)

    for code, r in sorted(listed.items()):
        record(code, r["corp_name"], r["stock_code"], {"modify_date": r["modify_date"]}, "changed")
    for code in sorted(gone):
        r: Optional[dict] = by_code.get(code)
        name = r["corp_name"] if r else json.loads(latest[code].value)["corp_name"]
        attrs = {"modify_date": r["modify_date"]} if r else {"removed_from_file": True}
        record(code, name, "", attrs, "code_removed")
    return out, counts
