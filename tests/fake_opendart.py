"""테스트용 가짜 OpenDART. 응답 형식은 개발가이드 명세(list.json, fnlttSinglAcntAll.json, corpCode.xml)를 따른다."""
import io
import json
import zipfile
from urllib.parse import urlparse

TEST_KEY = "TESTKEY-0000000000000000000000000000000"


class FakeDart:
    def __init__(self, key=TEST_KEY):
        self.key = key
        self.filings: list[dict] = []          # list.json 행
        self.fs: dict[tuple, list] = {}        # (corp_code, bsns_year, reprt_code, fs_div) → 행
        self.xbrl: dict[str, bytes] = {}       # rcept_no → XBRL zip (없으면 XML 오류 013)
        self.corps: list[tuple] = []           # corpCode.xml 행: (corp_code, corp_name, stock_code, modify_date)
        self.calls: list[tuple] = []
        self.force_status = None               # 예: "020"이면 모든 호출에 그 상태

    def filing(self, rcept_no, corp_code, report_nm, corp_cls="Y", rm="유", corp_name="테스트", stock_code="005930",
               flr_nm="테스트"):
        row = {"corp_cls": corp_cls, "corp_name": corp_name, "corp_code": corp_code, "stock_code": stock_code,
               "report_nm": report_nm, "rcept_no": rcept_no, "flr_nm": flr_nm, "rcept_dt": rcept_no[:8], "rm": rm}
        self.filings = [f for f in self.filings if f["rcept_no"] != rcept_no] + [row]
        return row

    def statement(self, rcept_no, corp_code, bsns_year, reprt_code, fs_div, accounts):
        """accounts: [(sj_div, account_id, account_nm, thstrm_amount, thstrm_add_amount 또는 None), …]"""
        rows = []
        for i, (sj, aid, anm, amt, add) in enumerate(accounts):
            row = {"rcept_no": rcept_no, "reprt_code": reprt_code, "bsns_year": str(bsns_year), "corp_code": corp_code,
                   "sj_div": sj, "sj_nm": sj, "account_id": aid, "account_nm": anm, "thstrm_nm": "제 1 기",
                   "thstrm_amount": amt, "ord": str(i + 1), "currency": "KRW"}
            if add is not None:
                row["thstrm_add_amount"] = add
            rows.append(row)
        self.fs[(corp_code, str(bsns_year), reprt_code, fs_div)] = rows

    def __call__(self, url, params, timeout):
        endpoint = urlparse(url).path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, dict(params)))
        if params.get("crtfc_key") != self.key:
            return self._json({"status": "010", "message": "등록되지 않은 키입니다."})
        if self.force_status:
            return self._json({"status": self.force_status, "message": "강제 오류"})
        if endpoint == "list.json":
            return self._list(params)
        if endpoint == "fnlttSinglAcntAll.json":
            rows = self.fs.get((params["corp_code"], params["bsns_year"], params["reprt_code"], params["fs_div"]))
            if not rows:
                return self._json({"status": "013", "message": "조회된 데이타가 없습니다."})
            return self._json({"status": "000", "message": "정상", "list": rows})
        if endpoint == "corpCode.xml":
            return 200, {"Content-Type": "application/x-msdownload"}, corp_code_zip(self.corps)
        if endpoint == "fnlttXbrl.xml":
            payload = self.xbrl.get(params["rcept_no"])
            if payload is None:
                body = '<?xml version="1.0" encoding="UTF-8"?><result><status>013</status><message>조회된 데이타가 없습니다.</message></result>'
                return 200, {"Content-Type": "application/xml"}, body.encode("utf-8")
            return 200, {"Content-Type": "application/x-msdownload"}, payload
        return 404, {}, b""

    def _list(self, p):
        rows = sorted((f for f in self.filings if p["bgn_de"] <= f["rcept_dt"] <= p["end_de"]
                       and f["corp_cls"] == p.get("corp_cls", f["corp_cls"])), key=lambda f: f["rcept_no"])
        if not rows:
            return self._json({"status": "013", "message": "조회된 데이타가 없습니다."})
        size, page = int(p.get("page_count", 10)), int(p.get("page_no", 1))
        total_page = -(-len(rows) // size)
        return self._json({"status": "000", "message": "정상", "page_no": page, "page_count": size,
                           "total_count": len(rows), "total_page": total_page,
                           "list": rows[(page - 1) * size: page * size]})

    @staticmethod
    def _json(d):
        return 200, {"Content-Type": "application/json"}, json.dumps(d, ensure_ascii=False).encode("utf-8")


def corp_code_zip(corps) -> bytes:
    """(corp_code, corp_name, stock_code, modify_date) 목록 → 실제와 같은 모양의 zip (CORPCODE.xml 하나)."""
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<result>"]
    for code, name, stock, mod in corps:
        xml.append(f"<list><corp_code>{code}</corp_code><corp_name>{name}</corp_name>"
                   f"<corp_eng_name>X</corp_eng_name><stock_code>{stock or ' '}</stock_code>"
                   f"<modify_date>{mod}</modify_date></list>")
    xml.append("</result>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("CORPCODE.xml", "\n".join(xml))
    return buf.getvalue()
