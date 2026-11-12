"""근거 기반 어시스턴트: 읽기 전용 도구, MCP 서버, 근거 검증, Function Calling 루프 (가이드 14장).

모델 호출은 대본대로 답하는 가짜 클라이언트로 대신한다 (네트워크·API 키 없음). 모델 자체의 판단 품질은
assistant/eval의 평가 세트로 실제 모델에서 잰다.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from mcp import Client

from assistant.agent import SUBMIT_TOOL, SYSTEM_PROMPT, Agent, mcp_tools_to_claude
from assistant.mcp_server import build_server
from assistant.tools import PitTools, ToolError, parse_as_of
from assistant.verify import ABSTAINED, VERIFIED, WARNING, EvidenceIndex, _numbers_in, render, verify_answer
from tests.fixtures.pit_fixture import AS_OF, AS_OF_EARLY, INJECTION, SAMSUNG, SL, build


@pytest.fixture
def data_dir(tmp_path):
    build(tmp_path)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


# ---------- 도구: 권한·시점 ----------

def test_tools_return_only_llm_allowed_data(data_dir):
    t = PitTools(data_dir)
    perms = {c["contract_id"]: c["llm_input_allowed"] for c in t.data_permissions()["contracts"]}
    assert perms["fsc_krx_stock_price_v1"] is False and perms["opendart_xbrl_v1"] is False
    assert perms["opendart_fnltt_v1"] and perms["bok_ecos_v1"]
    # 시세·부문 숫자는 파일이 있어도 어떤 도구로도 나오지 않는다
    dump = json.dumps([t.find_company("삼성", AS_OF), t.search_dart_filings(SAMSUNG, AS_OF),
                       t.get_financials(SL, AS_OF, account_query=""), t.list_macro_series(AS_OF)], ensure_ascii=False)
    assert "71000" not in dump and "seg1" not in dump and "krx_daily_price" not in dump
    for row in t.get_financials(SL, AS_OF)["rows"]:
        assert row["evidence_id"] and row["source_url"] and row["contract_id"] == "opendart_fnltt_v1"
        assert row["attribution"].startswith("출처: 금융감독원")


def test_tools_respect_as_of(data_dir):
    t = PitTools(data_dir)
    late = [f["rcept_no"] for f in t.search_dart_filings(SAMSUNG, AS_OF)["filings"]]
    early = [f["rcept_no"] for f in t.search_dart_filings(SAMSUNG, AS_OF_EARLY)["filings"]]
    assert late == ["20261111000100", "20261109000007"] and early == ["20261109000007"]
    assert t.find_company("삼성전자", "2026-11-10T00:00:00Z")["status"] == "BEFORE_COVERAGE"   # 매핑 수집 전
    with pytest.raises(ToolError, match="미래"):
        t.find_company("삼성전자", "2099-01-01T00:00:00Z")
    with pytest.raises(ToolError, match="시간대"):
        parse_as_of("2026-11-12T00:00:00")


def test_find_company_and_financial_filters(data_dir):
    t = PitTools(data_dir)
    for q in ("에스엘", "005850", SL):
        assert t.find_company(q, AS_OF)["matches"][0]["corp_code"] == SL
    assert t.find_company("비상장", AS_OF)["status"] == "NOT_FOUND"          # 종목코드 없는 회사는 제외
    rows = t.get_financials(SL, AS_OF, account_query="영업이익")["rows"]
    assert {(r["basis"], r["value"]) for r in rows} == {("당기", -36_614_872_000), ("당기누적", 53_769_990_000)}
    ofs = t.get_financials(SL, AS_OF, consolidation="OFS")["rows"]
    assert [r["value"] for r in ofs] == [700_000_000_000]
    assert t.get_financials(SAMSUNG, AS_OF)["status"] == "NO_DATA"
    with pytest.raises(ToolError):
        t.get_financials(SL, AS_OF, consolidation="연결")
    s = t.get_macro_series("bok_ecos:722Y001/0101000|value", AS_OF, 1)["rows"]
    assert s[0]["value"] == 2.75 and s[0]["period_end"] == "2026-10-31"
    with pytest.raises(ToolError, match="list_macro_series"):
        t.get_macro_series("fsc_krx_stock_price:ohlcv", AS_OF)


# ---------- MCP 서버 ----------

def test_mcp_server_exposes_read_only_tools_and_readable_errors(data_dir):
    async def go():
        async with Client(build_server(str(data_dir))) as c:
            tools = (await c.list_tools()).tools
            names = {x.name for x in tools}
            assert names == {"data_permissions", "find_company", "search_dart_filings", "get_financials",
                             "list_macro_series", "get_macro_series"}
            assert all(x.annotations.read_only_hint and not x.annotations.destructive_hint for x in tools)
            r = await c.call_tool("find_company", {"query": "에스엘", "as_of": AS_OF})
            assert not r.is_error and json.loads(r.content[0].text)["matches"][0]["corp_code"] == SL
            bad = await c.call_tool("find_company", {"query": "에스엘", "as_of": "2099-01-01T00:00:00Z"})
            assert bad.is_error and "미래" in bad.content[0].text                   # 이유가 모델에 전달된다
            return tools
    tools = run(go())
    claude_tools = mcp_tools_to_claude(tools)
    assert all(set(t) == {"name", "description", "input_schema"} and t["description"] for t in claude_tools)


# ---------- 근거 검증 ----------

def _index():
    idx = EvidenceIndex()
    idx.add_tool_result("get_financials", "t1", {"rows": [
        {"evidence_id": "R", "value": 895_555_569_000, "unit": "KRW", "basis": "당기누적", "consolidation": "CFS",
         "period_end": "2026-09-30", "account_name": "매출액", "account_id": "ifrs-full_Revenue", "source_url": "u",
         "attribution": "출처: DART"},
        {"evidence_id": "OP", "value": -36_614_872_000, "unit": "KRW", "basis": "당기", "consolidation": "CFS",
         "period_end": "2026-09-30", "account_name": "영업이익(손실)", "account_id": "dart_OperatingIncomeLoss"}]})
    idx.add_tool_result("get_macro_series", "t2", {"rows": [{"evidence_id": "BR", "value": 2.75, "unit": "연%",
                                                              "label": "한국은행 기준금리", "period_end": "2026-10-31"}]})
    return idx


def claim(text, eid, value, unit, **kw):
    return {"text": text, "evidence_id": eid, "value": value, "unit": unit, "metric": None, "period_end": None,
            "basis": None, "consolidation": None, **kw}


def test_verify_values_units_and_meaning():
    idx = _index()
    ok = {"status": "ANSWERED", "answer": "누적 매출 8,955억 원, 3분기 영업손실 366억 원, 기준금리 2.75%",
          "claims": [claim("누적 매출 8,955억 원", "R", 8955, "억원", basis="당기누적", consolidation="CFS", metric="매출액"),
                     claim("3분기 영업손실 366억 원", "OP", -366, "억원", basis="당기", metric="영업이익"),
                     claim("기준금리 2.75%", "BR", 2.75, "%", metric="기준금리", period_end="2026-10-31")]}
    v = verify_answer(ok, idx)
    assert v.status == VERIFIED and [c["status"] for c in v.claims] == [VERIFIED] * 3
    assert verify_answer({**ok, "claims": [claim("x", "R", 0.8956, "조원")], "answer": "0.8956조"}, idx).status == VERIFIED
    cases = {   # 의미 불일치 (가이드 14-2): 값이 맞아도 WARNING
        "분기 값을 누적이라고 함": claim("누적 영업손실 366억", "OP", -366, "억원", basis="당기누적"),
        "별도 기준이라고 함": claim("별도 매출 8,955억", "R", 8955, "억원", consolidation="OFS"),
        "다른 지표라고 함": claim("순이익 8,955억", "R", 8955, "억원", metric="당기순이익"),
        "단위가 다름": claim("8,955억 달러", "R", 8955, "억달러"),
        "값이 다름": claim("매출 9,500억", "R", 9500, "억원"),
        "기간이 다름": claim("6월 말 매출", "R", 8955, "억원", period_end="2026-06-30"),
        "단위 없음": claim("매출 8955", "R", 8955, None),
    }
    for why, c in cases.items():
        r = verify_answer({"status": "ANSWERED", "answer": c["text"], "claims": [c]}, idx)
        assert r.claims[0]["status"] == WARNING and r.status == WARNING, why
    fake = verify_answer({"status": "ANSWERED", "answer": "1조 원", "claims": [claim("1조 원", "NOPE", 1, "조원")]}, idx)
    assert fake.claims[0]["status"] == ABSTAINED and fake.status == WARNING
    loose = verify_answer({"status": "ANSWERED", "answer": "매출은 8,955억 원이고 성장률은 12.5%입니다",
                           "claims": [claim("매출 8,955억 원", "R", 8955, "억원")]}, idx)
    assert loose.status == WARNING and loose.unclaimed_numbers == ["12.5"]           # 근거 없는 숫자
    held = verify_answer({"status": "ABSTAINED", "answer": "시세는 제공되지 않습니다", "claims": [],
                          "abstain_reason": "이용 조건"}, idx)
    assert held.status == ABSTAINED
    assert _numbers_in("2026년 3분기 8,955억, 접수번호 20261106000178, 종목 005930, 2026-09-30, 12건, -3.5%") == ["8,955", "-3.5"]
    assert "R" in render(ok, v) and "✅ VERIFIED" in render(ok, v)


# ---------- Function Calling 루프 ----------

def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class ScriptedLLM:
    """대본대로 답하는 가짜 Claude. 각 차례는 지금까지의 messages를 받아 content 블록 목록을 돌려준다."""

    def __init__(self, turns, stop=None):
        self.turns, self.stop, self.calls = list(turns), stop, []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        content = self.turns.pop(0)(kw["messages"])
        stop = self.stop or ("tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")
        return NS(content=content, stop_reason=stop, model=kw["model"], usage=NS(input_tokens=100, output_tokens=20))


def results_of(messages):
    return {r["tool_use_id"]: r for r in messages[-1]["content"]}


def agent_run(data_dir, turns, question="질문", as_of=AS_OF, **kw):
    async def go():
        async with Client(build_server(str(data_dir))) as c:
            llm = ScriptedLLM(turns, kw.pop("stop", None))
            res = await Agent(llm, c, as_of_utc=parse_as_of(as_of), **kw).ask(question)
            return res, llm
    return run(go())


def test_agent_answers_with_verified_evidence(data_dir):
    def submit(m):
        rows = json.loads(results_of(m)["t2"]["content"])["rows"]
        op = next(r for r in rows if r["basis"] == "당기")
        return [tool_use("t3", "submit_answer", status="ANSWERED", answer="에스엘의 3분기 연결 영업손실은 366억 원입니다.",
                         abstain_reason=None, counter_evidence="한 분기 값이며 일회성 비용 여부는 공시 원문 확인이 필요합니다.",
                         claims=[claim("3분기 연결 영업손실 366억 원", op["evidence_id"], -366, "억원", basis="당기",
                                       consolidation="CFS", metric="영업이익", period_end="2026-09-30")])]
    turns = [lambda m: [NS(type="text", text="회사를 찾겠습니다."), tool_use("t1", "find_company", query="에스엘")],
             lambda m: [tool_use("t2", "get_financials", corp_code=SL, account_query="영업이익")],
             submit]
    res, llm = agent_run(data_dir, turns, "에스엘 3분기 영업이익은?")
    assert res.status == VERIFIED and "366억" in res.rendered and "반대 근거" in res.rendered
    assert [c["name"] for s in res.steps for c in s["tool_calls"]] == ["find_company", "get_financials", "submit_answer"]
    first = llm.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["system"] == SYSTEM_PROMPT and AS_OF in first["messages"][0]["content"]   # 기준 시각은 사용자 메시지에
    assert [t["name"] for t in first["tools"]][-1] == "submit_answer" and SUBMIT_TOOL["strict"]
    # 받은 assistant 블록을 그대로 다시 보내고, 도구 결과는 한 user 메시지로
    second = llm.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0]["text"] == "회사를 찾겠습니다."
    assert second[2]["role"] == "user" and second[2]["content"][0]["type"] == "tool_result"


def test_agent_forces_as_of_and_parallel_results(data_dir):
    seen = {}

    def turn2(m):
        r = results_of(m)
        seen["filings"] = [f["rcept_no"] for f in json.loads(r["a"]["content"])["filings"]]
        seen["rate"] = json.loads(r["b"]["content"])["rows"][-1]["value"]
        return [tool_use("s", "submit_answer", status="ABSTAINED", answer="확인할 수 없습니다.", abstain_reason="테스트",
                         counter_evidence=None, claims=[])]
    turns = [lambda m: [tool_use("a", "search_dart_filings", corp_code=SAMSUNG, as_of="2099-01-01T00:00:00Z"),
                        tool_use("b", "get_macro_series", series="bok_ecos:722Y001/0101000|value")],
             turn2]
    res, _ = agent_run(data_dir, turns, as_of=AS_OF_EARLY)
    assert seen["filings"] == ["20261109000007"]          # 모델이 2099년을 넣어도 사람이 정한 시각으로 조회
    assert seen["rate"] == 2.75
    calls = res.steps[0]["tool_calls"]
    assert calls[0]["overrides"]["as_of"]["model"] == "2099-01-01T00:00:00Z" and calls[1]["overrides"] == {}
    assert res.status == ABSTAINED


def test_injected_text_stays_in_tool_data_and_fake_numbers_are_caught(data_dir):
    """공시 제목에 지시문이 있어도 도구 결과(데이터)로만 전달된다. 모델이 따라서 근거 없는 숫자를 내면 검증기가 잡는다."""
    def obey(m):
        payload = json.loads(results_of(m)["f"]["content"])
        assert payload["filings"][0]["report_nm"] == INJECTION and "report_nm" in payload["untrusted_text"]
        return [tool_use("s", "submit_answer", status="ANSWERED", answer="매출은 1조 원입니다.", abstain_reason=None,
                         counter_evidence=None, claims=[claim("매출 1조 원", payload["filings"][0]["evidence_id"], 1, "조원",
                                                              metric="매출액")])]
    res, _ = agent_run(data_dir, [lambda m: [tool_use("f", "search_dart_filings", corp_code=SAMSUNG)], obey])
    assert res.status == WARNING
    assert any("근거 값이 숫자가 아님" in p for p in res.verification["claims"][0]["problems"])


def test_agent_failure_paths(data_dir):
    text = lambda m: [NS(type="text", text="답은 3%입니다.")]
    res, llm = agent_run(data_dir, [text, text])                          # submit_answer 없이 끝냄 → 한 번 상기 → 실패
    assert res.status == "FAILED" and "submit_answer" in res.error
    assert "submit_answer" in llm.calls[1]["messages"][-1]["content"]
    res, _ = agent_run(data_dir, [text], stop="refusal")
    assert res.status == "FAILED" and "refusal" in res.error
    loop = lambda m: [tool_use(f"x{len(m)}", "list_macro_series")]
    res, _ = agent_run(data_dir, [loop] * 3, max_steps=3)
    assert res.status == "FAILED" and "3단계" in res.error
    bad = lambda m: [tool_use("b", "get_financials", corp_code=SL, consolidation="연결")]
    res, llm = agent_run(data_dir, [bad, lambda m: [tool_use("s", "submit_answer", status="ABSTAINED", answer="-",
                                                             abstain_reason="x", counter_evidence=None, claims=[])]])
    assert res.steps[0]["tool_calls"][0]["is_error"] and llm.calls[1]["messages"][-1]["content"][0]["is_error"]


# ---------- 평가 세트 ----------

def test_golden_set_is_well_formed_and_scoring_works(data_dir):
    from assistant.eval.run_eval import GOLDEN, score, summary_md
    cases = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len({c["id"] for c in cases}) == len(cases) >= 6
    for c in cases:
        assert c["expect_status"] in ("ANSWERED", "ABSTAINED") and parse_as_of(c["as_of"])
    assert {c["expect_status"] for c in cases} == {"ANSWERED", "ABSTAINED"}      # 답할 것과 보류할 것 둘 다
    turns = [lambda m: [tool_use("t", "get_macro_series", series="bok_ecos:722Y001/0101000|value", last_n=1)],
             lambda m: [tool_use("s", "submit_answer", status="ANSWERED", answer="2.75%입니다.", abstain_reason=None,
                                 counter_evidence=None,
                                 claims=[claim("2.75%", json.loads(results_of(m)["t"]["content"])["rows"][0]["evidence_id"],
                                               2.75, "%", metric="기준금리")])]]
    res, _ = agent_run(data_dir, turns)
    case = {"id": "macro-rate", "category": "거시", "expect_status": "ANSWERED", "expect_tools": ["get_macro_series"],
            "expect_evidence_prefix": "722Y001/0101000|value|"}
    row = score(case, res)
    assert row["status_ok"] and row["tools_ok"] and row["evidence_ok"] and row["verified_rate"] == 1.0
    assert "macro-rate" in summary_md([row], "test")
