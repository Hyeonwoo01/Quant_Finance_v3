"""OpenDART 재무제표 원본파일(XBRL) 받기와 해석 (가이드 5-3 사업부문 노출도의 입력).

API: fnlttXbrl.xml (rcept_no, reprt_code) → zip(인스턴스 문서 .xbrl, 확장 택사노미 .xsd, 레이블 lab-ko.xml 등).
오류는 zip 대신 XML(<result><status>…)로 온다 (개발가이드 2026-11-09 확인).

원본 zip에는 서술형 주석(공시 제출인의 저작물일 수 있음)이 들어 있어 저장하지 않는다. 메모리에서 해석해 숫자 사실
(부문별·지역별 매출·영업이익 등)만 뽑고, 원본은 해시와 접수번호로만 추적한다 (계약 opendart_xbrl_v1).
"""
import argparse
import hashlib
import io
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from collectors.base_client import CallBudget
from collectors.opendart import OpenDartClient, OpenDartError

NS = {
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xbrldi": "http://xbrl.org/2006/xbrldi",
    "link": "http://www.xbrl.org/2003/linkbase",
    "xlink": "http://www.w3.org/1999/xlink",
}
_SKIP_NS = {NS["xbrli"], NS["link"], "http://www.xbrl.org/2003/XLink", NS["xlink"]}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass(frozen=True)
class Context:
    id: str
    start: Optional[date]
    end: Optional[date]          # duration의 끝 또는 instant
    instant: bool
    dims: tuple                  # ((축 QName, 멤버 QName), …) 정렬됨


@dataclass(frozen=True)
class Fact:
    concept: str                 # prefix:local
    context: Context
    value: Optional[float]       # 숫자가 아니면 None (서술형 사실은 값을 담지 않는다)
    unit: Optional[str]
    decimals: Optional[str]


@dataclass
class XbrlDoc:
    files: list
    contexts: dict = field(default_factory=dict)
    facts: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)        # prefix:local → 한국어 레이블
    payload_sha256: str = ""

    def label(self, qname: str) -> str:
        return self.labels.get(qname) or qname.split(":", 1)[-1]


def _date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return date.fromisoformat(s.strip()[:10])


def _qname(tag: str, ns_prefix: dict) -> str:
    """'{uri}local' → 'prefix:local'."""
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return f"{ns_prefix.get(uri, uri)}:{local}"
    return tag


def parse_xbrl_zip(payload: bytes) -> XbrlDoc:
    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = zf.namelist()
    doc = XbrlDoc(files=names, payload_sha256=hashlib.sha256(payload).hexdigest())
    inst = [n for n in names if n.lower().endswith(".xbrl")]
    if not inst:
        raise ValueError(f"인스턴스 문서(.xbrl)가 없음: {names}")
    raw = zf.read(inst[0])

    ns_prefix: dict[str, str] = {}
    for _, (prefix, uri) in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
        ns_prefix.setdefault(uri, prefix or "")
    root = ET.fromstring(raw)

    for c in root.findall("xbrli:context", NS):
        dims = []
        for holder in (c.find("xbrli:entity/xbrli:segment", NS), c.find("xbrli:scenario", NS)):
            if holder is None:
                continue
            for m in holder.findall("xbrldi:explicitMember", NS):
                dims.append((m.get("dimension", "").strip(), (m.text or "").strip()))
            for m in holder.findall("xbrldi:typedMember", NS):
                dims.append((m.get("dimension", "").strip(), "typed:" + "".join(m.itertext()).strip()))
        p = c.find("xbrli:period", NS)
        instant = p.find("xbrli:instant", NS) if p is not None else None
        if instant is not None:
            ctx = Context(c.get("id"), None, _date(instant.text), True, tuple(sorted(dims)))
        else:
            ctx = Context(c.get("id"), _date(p.findtext("xbrli:startDate", namespaces=NS)) if p is not None else None,
                          _date(p.findtext("xbrli:endDate", namespaces=NS)) if p is not None else None, False,
                          tuple(sorted(dims)))
        doc.contexts[ctx.id] = ctx

    for el in root:
        if not el.tag.startswith("{"):
            continue
        uri = el.tag[1:].split("}", 1)[0]
        ref = el.get("contextRef")
        if uri in _SKIP_NS or ref is None or ref not in doc.contexts:
            continue
        nil = el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
        value = None
        if not nil and el.get("unitRef") is not None:
            try:
                value = float((el.text or "").strip())
            except ValueError:
                value = None
        doc.facts.append(Fact(_qname(el.tag, ns_prefix), doc.contexts[ref], value, el.get("unitRef"), el.get("decimals")))

    for n in names:
        if re.search(r"lab[-_]?ko", n.lower()) or (n.lower().endswith("_lab.xml")):
            doc.labels.update(_parse_labels(zf.read(n), lang="ko"))
    return doc


def _parse_labels(raw: bytes, lang: str) -> dict:
    """레이블 링크베이스 → {prefix:local: 레이블}. href의 '#prefix_local'을 QName으로 바꾼다 (표준 레이블 우선)."""
    root = ET.fromstring(raw)
    out = {}
    for ll in root.iter(f"{{{NS['link']}}}labelLink"):
        locs, texts, arcs = {}, defaultdict(list), []
        for el in ll:
            kind = el.get(f"{{{NS['xlink']}}}type")
            lab = el.get(f"{{{NS['xlink']}}}label")
            if kind == "locator":
                frag = el.get(f"{{{NS['xlink']}}}href", "").split("#", 1)[-1]
                locs[lab] = frag.replace("_", ":", 1) if "_" in frag else frag
            elif kind == "resource" and (el.get(XML_LANG, lang) or lang).startswith(lang):
                role = el.get(f"{{{NS['xlink']}}}role", "")
                texts[lab].append((0 if role.endswith("/label") else 1, (el.text or "").strip()))
            elif kind == "arc":
                arcs.append((el.get(f"{{{NS['xlink']}}}from"), el.get(f"{{{NS['xlink']}}}to")))
        for frm, to in arcs:
            if frm in locs and texts.get(to):
                out.setdefault(locs[frm], sorted(texts[to])[0][1])
    return out


def fetch_xbrl(client: OpenDartClient, rcept_no: str, reprt_code: str) -> bytes:
    """원본 zip 바이트. 오류 응답(XML·JSON)은 OpenDartError로."""
    resp = client.http.get("fnlttXbrl.xml", {"crtfc_key": client._key, "rcept_no": rcept_no, "reprt_code": reprt_code})
    body = resp.body
    if body[:2] == b"PK":
        return body
    status, message = "parse", "zip이 아닌 응답"
    try:
        root = ET.fromstring(body)
        status, message = root.findtext("status") or status, root.findtext("message") or message
    except ET.ParseError:
        pass
    raise OpenDartError(status, message, "fnlttXbrl.xml")


# ---------------- 구조 점검 (probe) ----------------

CONSOLIDATION_AXIS = "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
SEGMENT_AXIS = "ifrs-full:SegmentsAxis"
SEGMENT_ITEMS_AXIS = "ifrs-full:SegmentConsolidationItemsAxis"
GEO_AXIS = "ifrs-full:GeographicalAreasAxis"
PRODUCT_AXIS = "ifrs-full:ProductsAndServicesAxis"


def split_dims(ctx: Context) -> tuple[Optional[str], tuple]:
    """(연결·별도 멤버, 나머지 차원). DART XBRL은 거의 모든 사실에 연결·별도 차원을 붙인다."""
    cons, other = None, []
    for a, m in ctx.dims:
        if a == CONSOLIDATION_AXIS:
            cons = m
        else:
            other.append((a, m))
    return cons, tuple(other)


def summarize(doc: XbrlDoc, max_lines: int = 80) -> list[str]:
    """부문·지역·제품 축이 어떤 모양으로 쓰였는지와 당기 주요 값. 원문 서술은 출력하지 않는다."""
    num = [f for f in doc.facts if f.value is not None]
    lines = [f"사실 {len(doc.facts)}개(숫자 {len(num)}개), 레이블 {len(doc.labels)}개, sha256 {doc.payload_sha256[:12]}"]
    dur = [f for f in num if not f.context.instant and f.context.start and f.context.end]
    if not dur:
        return lines
    cur_end = max(f.context.end for f in dur)
    longest = min(f.context.start for f in dur if f.context.end == cur_end)       # 당기 누적(가장 긴) 기간
    cur = [f for f in dur if f.context.end == cur_end and f.context.start == longest]
    lines.append(f"당기 {longest}~{cur_end}, 당기 숫자 사실 {len(cur)}개")
    for axis in (SEGMENT_AXIS, GEO_AXIS, PRODUCT_AXIS):
        hits = [(f, *split_dims(f.context)) for f in cur if any(a == axis for a, _ in f.context.dims)]
        if not hits:
            continue
        shapes = Counter((cons.split(":")[-1] if cons else None, tuple(a.split(":")[-1] for a, _ in other))
                         for _, cons, other in hits)
        lines.append(f"[{axis}] 당기 사실 {len(hits)}개, 차원 모양: " +
                     "; ".join(f"{c}/{'+'.join(o)}={n}" for (c, o), n in shapes.most_common(5)))
        lines.append("   개념: " + ", ".join(f"{c}={n}" for c, n in Counter(f.concept for f, _, _ in hits).most_common(10)))
        items = Counter(m for _, _, other in hits for a, m in other if a == SEGMENT_ITEMS_AXIS)
        if items:
            lines.append("   통합항목 멤버: " + ", ".join(f"{m}({doc.label(m)})={n}" for m, n in items.most_common(6)))
        best_cons = Counter(cons for _, cons, _ in hits).most_common(1)[0][0]
        members = defaultdict(dict)
        for f, cons, other in hits:
            od = dict(other)
            if cons != best_cons or set(od) - {axis, SEGMENT_ITEMS_AXIS}:
                continue
            members[od[axis]][(f.concept, od.get(SEGMENT_ITEMS_AXIS))] = f.value
        for mem, vals in list(members.items())[:14]:
            def pick(pat):
                for (c, item), v in vals.items():
                    if re.search(pat, c) and item in (None, "ifrs-full:OperatingSegmentsMember"):
                        return f"{v:,.0f}"
                return None
            lines.append(f"   {mem} ({doc.label(mem)}): 매출 {pick(r'Revenue|Sales')}, "
                         f"영업이익 {pick(r'OperatingIncome|ProfitLossFromOperating|OperatingProfit')}, 사실 {len(vals)}개")
    return lines[:max_lines]


def _find_reports(client: OpenDartClient, stock_codes: list, detail_ty: str, bgn: date, end: date) -> dict:
    """그 기간에 접수된 정기보고서 중 종목코드별 가장 늦은 최초 제출본 접수번호."""
    found = {}
    page = 1
    while True:
        r = client.call("list.json", {"bgn_de": f"{bgn:%Y%m%d}", "end_de": f"{end:%Y%m%d}", "corp_cls": "Y",
                                      "pblntf_ty": "A", "pblntf_detail_ty": detail_ty, "page_no": page,
                                      "page_count": 100})
        for row in r.rows:
            sc = (row.get("stock_code") or "").strip()
            if sc in stock_codes and not row["report_nm"].startswith("["):
                found[sc] = (row["rcept_no"], row["corp_name"], row["report_nm"])
        if r.status != "000" or page >= int(r.data.get("total_page") or 1):
            return found
        page += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OpenDART XBRL 구조 점검 (원본은 저장하지 않음)")
    ap.add_argument("--stocks", default="005930,000660,005380,051910,005490,009540,012450,035420,006400,000270,"
                                       "066570,096770,028260,373220,068270")
    ap.add_argument("--annual", default="2026-03-01:2026-04-15", help="사업보고서 접수일 범위")
    ap.add_argument("--half", default="2026-08-01:2026-08-31", help="반기보고서 접수일 범위")
    ap.add_argument("--max-calls", type=int, default=200)
    args = ap.parse_args(argv)
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("DART_API_KEY 없음", file=sys.stderr)
        return 2
    client = OpenDartClient(key, budget=CallBudget(args.max_calls))
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    for detail, rng, code in (("A001", args.annual, "11011"), ("A002", args.half, "11012")):
        bgn, end = (date.fromisoformat(x) for x in rng.split(":"))
        reports = _find_reports(client, stocks, detail, bgn, end)
        print(f"===== {detail} {bgn}~{end}: {len(reports)}/{len(stocks)}개 보고서 찾음")
        for sc, (rcept_no, name, rnm) in sorted(reports.items()):
            print(f"--- {name} ({sc}) {rnm} {rcept_no}")
            try:
                t0 = time.monotonic()
                payload = fetch_xbrl(client, rcept_no, code)
                t1 = time.monotonic()
                doc = parse_xbrl_zip(payload)
                print(f"   받기 {t1 - t0:.1f}초 ({len(payload) / 1e6:.1f}MB), 해석 {time.monotonic() - t1:.1f}초")
            except (OpenDartError, ValueError, zipfile.BadZipFile) as e:
                print(f"   실패: {e}")
                continue
            for line in summarize(doc):
                print("   " + line)
    print(f"호출 {client.budget.used}회")
    return 0


if __name__ == "__main__":
    sys.exit(main())
