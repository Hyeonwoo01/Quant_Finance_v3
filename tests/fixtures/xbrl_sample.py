"""테스트용 합성 XBRL zip (DART XBRL 구조: 인스턴스 + 한국어 레이블 링크베이스). 값은 꾸민 숫자다."""
import io
import zipfile

ENTITY = "entity00126380"


def _ctx(cid, start, end, dims=()):
    seg = ""
    if dims:
        members = "".join(f'<xbrldi:explicitMember dimension="{a}">{m}</xbrldi:explicitMember>' for a, m in dims)
        seg = f"<xbrli:segment>{members}</xbrli:segment>"
    period = (f"<xbrli:instant>{end}</xbrli:instant>" if start is None
              else f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://dart.fss.or.kr">00126380'
            f"</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>")


def build(facts, labels=None, contexts=None, extra_files=()):
    """facts: [(concept, context_id, value 또는 None(서술), unit 또는 None)], contexts: [(id, start, end, dims)]."""
    ctx_xml = "".join(_ctx(*c) for c in (contexts or []))
    fact_xml = ""
    for concept, cid, value, unit in facts:
        unit_attr = f' unitRef="{unit}" decimals="-6"' if unit else ""
        fact_xml += f'<{concept} contextRef="{cid}"{unit_attr}>{value if value is not None else "서술형 주석"}</{concept}>'
    inst = f"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2023-03-23/ifrs-full" xmlns:dart="http://dart.fss.or.kr/2024"
 xmlns:{ENTITY}="http://dart.fss.or.kr/{ENTITY}" xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
{ctx_xml}<xbrli:unit id="KRW"><xbrli:measure>iso4217:KRW</xbrli:measure></xbrli:unit>{fact_xml}</xbrli:xbrl>"""
    locs = "".join(
        f'<link:loc xlink:type="locator" xlink:href="{ENTITY}.xsd#{q.replace(":", "_", 1)}" xlink:label="loc{i}"/>'
        f'<link:label xlink:type="resource" xlink:label="lab{i}" xlink:role="http://www.xbrl.org/2003/role/label" '
        f'xml:lang="ko">{text}</link:label><link:labelArc xlink:type="arc" xlink:from="loc{i}" xlink:to="lab{i}"/>'
        for i, (q, text) in enumerate((labels or {}).items()))
    lab = (f'<?xml version="1.0" encoding="utf-8"?><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
           f'xmlns:xlink="http://www.w3.org/1999/xlink"><link:labelLink xlink:type="extended">{locs}'
           f"</link:labelLink></link:linkbase>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{ENTITY}_2025-12-31.xbrl", inst.encode("utf-8"))
        z.writestr(f"{ENTITY}_2025-12-31_lab-ko.xml", lab.encode("utf-8"))
        for name, data in extra_files:
            z.writestr(name, data)
    return buf.getvalue()
