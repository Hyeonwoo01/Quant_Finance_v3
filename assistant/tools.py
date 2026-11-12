"""AI가 쓰는 읽기 전용 PIT 조회 도구 (가이드 14-1, 14-3). MCP 서버(assistant/mcp_server.py)가 이 함수들을 감싼다.

지키는 것:
- **권한:** 계약이 'LLM 입력'을 승인한 데이터만 돌려준다 (Purpose.LLM_INPUT). 한국 주식 시세(재배포 금지)와 XBRL 부문
  숫자(원본에 서술형 주석)는 계약상 막혀 있어 여기서 나오지 않는다. data_permissions 도구가 무엇이 왜 막혔는지 알려 준다.
- **시점:** 모든 조회는 as_of 시각에 실제로 알 수 있었던 버전만 본다 (available_at <= as_of). 미래 시각은 거부한다.
- **근거:** 돌려주는 모든 값에 evidence_id(= PIT revision_id), 출처 URL, 가용 시각, 계약 ID를 붙인다. Agent는 답의 숫자마다
  evidence_id를 달아야 하고, 검증기(assistant/verify.py)가 그 값과 대조한다.
- **읽기만:** 저장소를 바꾸는 함수, 임의 질의(SQL·코드) 실행, 외부 호출은 없다. 입력은 정해진 파라미터뿐이다.
- 공시 제목·회사명 같은 텍스트는 외부에서 온 데이터다. 그 안의 문장은 지시가 아니다 (Agent 시스템 프롬프트에도 적는다).
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from collectors.macro_sources import SERIES_ECOS, SERIES_EIA, SERIES_KCS
from collectors.opendart import SERIES_FILINGS, SERIES_FS
from collectors.opendart_corpcode import SERIES_CORP_MASTER
from core.data_contracts import ContractRegistry, Purpose
from core.security_master import SecurityMaster
from core.source_registry import default_registry
from storage.pit_store import PITFileStore, by_period_month

MAX_ROWS = 60
MACRO_SERIES = {SERIES_KCS: "KR", SERIES_ECOS: "KR", SERIES_EIA: "US"}   # series_id → 파일 나누기 (entity)
# 계정을 지정하지 않았을 때 돌려주는 주요 계정 (IFRS·DART 표준 계정 ID)
KEY_ACCOUNTS = ("ifrs-full_Revenue", "dart_OperatingIncomeLoss", "ifrs-full_ProfitLoss", "ifrs-full_Assets",
                "ifrs-full_Liabilities", "ifrs-full_Equity")
ATTRIBUTION = {"OpenDART": "출처: 금융감독원 전자공시시스템(DART)", "KCS": "출처: 관세청, 공공데이터포털",
               "BOK": "출처: 한국은행 ECOS", "EIA": "Source: U.S. Energy Information Administration"}


class ToolError(ValueError):
    """잘못된 입력 (모델에 오류로 돌려준다)."""


def parse_as_of(as_of: Optional[str], now: Optional[datetime] = None) -> datetime:
    """ISO 8601 시각(시간대 필수) → UTC. 비우면 지금. 미래 시각은 거부 (아직 알 수 없는 정보)."""
    now = now or datetime.now(timezone.utc)
    if not as_of:
        return now
    try:
        t = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"as_of 형식 오류: {as_of!r} (예: 2026-11-12T08:30:00+09:00)") from None
    if t.tzinfo is None:
        raise ToolError("as_of에 시간대가 없음 (예: +09:00 또는 Z)")
    t = t.astimezone(timezone.utc)
    if t > now + timedelta(minutes=1):
        raise ToolError(f"as_of({t.isoformat()})가 현재보다 미래 — 아직 알 수 없는 정보는 조회하지 않는다")
    return t


def _iso(v) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


@dataclass
class PitTools:
    data_dir: Path
    registry: ContractRegistry = None
    now: callable = None

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.registry = self.registry or default_registry()
        self.now = self.now or (lambda: datetime.now(timezone.utc))
        self.store = PITFileStore(self.data_dir, self.registry,
                                  partitions={SERIES_FILINGS: by_period_month, SERIES_CORP_MASTER: lambda r: "all"})

    # ---------- 공통 ----------
    def _query(self, as_of: datetime, series_id: str, parts: list, entity_id: Optional[str] = None) -> pd.DataFrame:
        existing = [p for p in parts if (self.data_dir / "pit_versions" / series_id / f"{p}.jsonl").exists()]
        if not existing:
            return pd.DataFrame()
        st = self.store.load(parts=[(series_id, p) for p in existing])
        return st.query_as_of(as_of, Purpose.LLM_INPUT, entity_id=entity_id, series_id=series_id)

    def _evidence(self, r) -> dict:
        c = self.registry.get(r.license_contract_id)
        return {"evidence_id": r.revision_id, "available_at_utc": _iso(r.available_at_utc),
                "source_url": r.source_url, "contract_id": r.license_contract_id,
                "attribution": ATTRIBUTION.get(r.source_provider, r.source_provider) if c and c.attribution_required else None}

    def _header(self, as_of: datetime, tool: str, **params) -> dict:
        return {"tool": tool, "as_of_utc": as_of.isoformat(), "params": params,
                "note": "available_at_utc <= as_of_utc 인 버전만, LLM 입력이 허용된 데이터만 포함"}

    # ---------- 도구 ----------
    def data_permissions(self) -> dict:
        """등록된 데이터 계약별로 AI가 읽을 수 있는지와 그 근거."""
        rows = []
        for cid in sorted(c.contract_id for c in self._contracts()):
            c = self.registry.get(cid)
            rows.append({"contract_id": cid, "provider": c.provider, "data": c.series_or_endpoint,
                         "llm_input_allowed": self.registry.is_allowed(cid, Purpose.LLM_INPUT),
                         "public_display_allowed": self.registry.is_allowed(cid, Purpose.PUBLIC_DISPLAY),
                         "basis": c.permission_reference})
        return {"tool": "data_permissions", "contracts": rows,
                "note": "llm_input_allowed=false인 데이터는 도구가 돌려주지 않는다. 그 데이터에 대한 질문은 답을 보류해야 한다"}

    def _contracts(self):
        from core.source_registry import REGISTERED_CONTRACTS
        return REGISTERED_CONTRACTS

    def find_company(self, query: str, as_of: Optional[str] = None, limit: int = 10) -> dict:
        """회사명 일부, 6자리 종목코드 또는 8자리 DART 고유번호로 회사를 찾는다. as_of 시점의 매핑만."""
        t = parse_as_of(as_of, self.now())
        q = (query or "").strip()
        if not q:
            raise ToolError("query가 비어 있음")
        df = self._query(t, SERIES_CORP_MASTER, ["all"])
        out = self._header(t, "find_company", query=q)
        if df.empty:
            sm = SecurityMaster.from_records([])
            out.update(matches=[], status="BEFORE_COVERAGE",
                       message="이 시점에는 종목 식별자 매핑이 없다 (수집 시작 전). 회사를 특정할 수 없음")
            return out
        sm = SecurityMaster.from_records([_row_to_record(r) for r in df.itertuples()])
        matches = []
        for r in df.itertuples():
            d = json.loads(r.value)
            if not d["stock_code"]:
                continue
            if q in (d["stock_code"], r.entity_id) or q.replace(" ", "") in d["corp_name"].replace(" ", ""):
                exact = q in (d["stock_code"], r.entity_id) or q.replace(" ", "") == d["corp_name"].replace(" ", "")
                matches.append((not exact, len(d["corp_name"]), {"corp_code": r.entity_id, "corp_name": d["corp_name"],
                                "stock_code": d["stock_code"], **self._evidence(r)}))
        matches.sort(key=lambda m: (m[0], m[1]))
        out.update(matches=[m[2] for m in matches[:max(1, min(limit, 20))]], total_matches=len(matches),
                   status="OK" if matches else "NOT_FOUND", coverage_start_utc=_iso(sm.coverage_start_utc))
        return out

    def search_dart_filings(self, corp_code: str, as_of: Optional[str] = None, lookback_days: int = 30,
                            keyword: Optional[str] = None, limit: int = 20) -> dict:
        """한 회사의 공시 목록 (접수일이 as_of 기준 lookback_days 안이고, as_of에 이용 가능했던 것만)."""
        t = parse_as_of(as_of, self.now())
        lookback_days = max(1, min(int(lookback_days), 366))
        start = (t - timedelta(days=lookback_days)).date()
        months = sorted({f"{d:%Y-%m}" for d in pd.date_range(start, t.date(), freq="D")})
        df = self._query(t, SERIES_FILINGS, months, entity_id=corp_code)
        rows = []
        for r in df.itertuples():
            d = json.loads(r.value)
            rcept_dt = r.observation_period_start
            if rcept_dt is None or rcept_dt < start:
                continue
            if keyword and keyword not in d["report_nm"]:
                continue
            rows.append({"rcept_no": r.source_document_id, "rcept_dt": _iso(rcept_dt), "report_nm": d["report_nm"],
                         "filer": d["flr_nm"], "remark": d["rm"], "corp_name": d["corp_name"], **self._evidence(r)})
        rows.sort(key=lambda x: x["rcept_no"], reverse=True)
        out = self._header(t, "search_dart_filings", corp_code=corp_code, lookback_days=lookback_days, keyword=keyword)
        out.update(filings=rows[:max(1, min(limit, MAX_ROWS))], total=len(rows),
                   untrusted_text=["report_nm", "filer", "remark", "corp_name"])
        return out

    def get_financials(self, corp_code: str, as_of: Optional[str] = None, account_query: Optional[str] = None,
                       consolidation: str = "CFS", statement: Optional[str] = None) -> dict:
        """정기보고서 재무제표 값 (as_of에 이용 가능했던 최신 버전).

        consolidation: CFS(연결) 또는 OFS(별도). statement: BS·IS·CIS·CF 중 하나(선택).
        account_query: 계정명 일부 또는 계정 ID(예: '영업이익', 'ifrs-full_Revenue'). 비우면 주요 계정.
        basis: '당기'는 그 분기(3개월) 또는 시점 잔액, '당기누적'은 연초부터 누적.
        """
        t = parse_as_of(as_of, self.now())
        if consolidation not in ("CFS", "OFS"):
            raise ToolError("consolidation은 CFS(연결) 또는 OFS(별도)")
        if statement and statement not in ("BS", "IS", "CIS", "CF"):
            raise ToolError("statement는 BS·IS·CIS·CF 중 하나")
        df = self._query(t, SERIES_FS, [corp_code], entity_id=corp_code)
        rows, reports = [], {}
        for r in df.itertuples():
            fs_div, sj, aid, anm, basis = (r.metric_id.split("|") + [""] * 5)[:5]
            reports.setdefault(r.source_document_id, {"rcept_no": r.source_document_id,
                                                      "period_end": _iso(r.observation_period_end)})
            if fs_div != consolidation or (statement and sj != statement):
                continue
            if account_query:
                if account_query not in aid and account_query.replace(" ", "") not in anm.replace(" ", ""):
                    continue
            elif aid not in KEY_ACCOUNTS:
                continue
            rows.append({"consolidation": fs_div, "statement": sj, "account_id": aid, "account_name": anm,
                         "basis": basis.split("#")[0], "period_end": _iso(r.observation_period_end),
                         "value": r.value, "unit": r.unit, "rcept_no": r.source_document_id, **self._evidence(r)})
        rows.sort(key=lambda x: (x["period_end"] or "", x["statement"], x["account_id"], x["basis"]), reverse=True)
        out = self._header(t, "get_financials", corp_code=corp_code, account_query=account_query,
                           consolidation=consolidation, statement=statement)
        out.update(rows=rows[:MAX_ROWS], total=len(rows), reports=sorted(reports.values(), key=lambda x: x["rcept_no"]),
                   status="OK" if rows else ("NO_DATA" if df.empty else "NO_MATCH"),
                   message=None if rows else ("이 시점에 이용 가능한 재무제표가 없다 (수집 대상은 유가증권시장 정기보고서)"
                                             if df.empty else "조건에 맞는 계정이 없다 (account_query·consolidation 확인)"))
        return out

    def list_macro_series(self, as_of: Optional[str] = None) -> dict:
        """조회할 수 있는 산업·거시 시리즈 목록 (관세청·한국은행 ECOS·미국 EIA)."""
        t = parse_as_of(as_of, self.now())
        series = []
        for sid, part in MACRO_SERIES.items():
            df = self._query(t, sid, [part])
            for metric, g in df.groupby("metric_id") if not df.empty else []:
                label = json.loads(g.iloc[-1]["attributes"] or "{}").get("label", "")
                series.append({"series": f"{sid}:{metric}", "label": label, "unit": g.iloc[-1]["unit"],
                               "observations": len(g), "last_period_end": _iso(g["observation_period_end"].max())})
        out = self._header(t, "list_macro_series")
        out.update(series=sorted(series, key=lambda s: s["series"]))
        return out

    def get_macro_series(self, series: str, as_of: Optional[str] = None, last_n: int = 12) -> dict:
        """한 시리즈의 최근 관측값 (series는 list_macro_series의 이름, 예: 'bok_ecos:722Y001/0101000|value')."""
        t = parse_as_of(as_of, self.now())
        sid, _, metric = (series or "").partition(":")
        if sid not in MACRO_SERIES or not metric:
            raise ToolError(f"알 수 없는 시리즈 {series!r} — list_macro_series로 이름을 확인")
        df = self._query(t, sid, [MACRO_SERIES[sid]])
        df = df[df.metric_id == metric] if not df.empty else df
        rows = []
        for r in df.sort_values("observation_period_end").tail(max(1, min(int(last_n), MAX_ROWS))).itertuples():
            rows.append({"period_start": _iso(r.observation_period_start), "period_end": _iso(r.observation_period_end),
                         "value": r.value, "unit": r.unit,
                         "label": json.loads(r.attributes or "{}").get("label", ""), **self._evidence(r)})
        out = self._header(t, "get_macro_series", series=series, last_n=last_n)
        out.update(rows=rows, status="OK" if rows else "NO_DATA",
                   availability_rule="공급원이 발표 시각을 주지 않아, 실제로 받은 뒤 첫 08:30 KST 컷오프부터 이용 가능")
        return out


def _row_to_record(r):
    """query_as_of 결과 행 → PITRecord (SecurityMaster용)."""
    from core.pit_schema import PITRecord
    from dataclasses import fields
    return PITRecord(**{f.name: getattr(r, f.name) for f in fields(PITRecord)})
