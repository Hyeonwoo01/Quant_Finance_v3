"""Agent가 쓰는 LLM 공급자 어댑터: Anthropic Claude와 OpenAI GPT.

Agent 루프(assistant/agent.py)는 공급자를 모른다. 어댑터가 공급자별로 다른 부분만 맡는다.
- 도구 정의 형식 (Claude: name/description/input_schema, OpenAI: type=function + function{name, parameters})
- 요청 파라미터 (Claude: system·effort·거절 시 대체 모델, OpenAI: system 메시지·reasoning_effort)
- 응답 해석 (멈춘 이유, 도구 호출, 토큰)
- 대화 기록에 붙이는 방식 (Claude: 받은 블록 그대로 + tool_result 블록 한 메시지, OpenAI: assistant 메시지의
  tool_calls + 호출마다 role=tool 메시지)

멈춘 이유는 공통 값으로 바꾼다: tool_calls · end · refusal · truncated · pause.
도구·근거 검증·기준 시각 강제는 공급자와 무관하게 같다.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Optional

CLAUDE_MODEL = "claude-opus-5-5"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_OUTPUT_TOKENS = 16000


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    parse_error: Optional[str] = None       # 인자 JSON을 읽지 못했을 때 (OpenAI는 인자를 문자열로 준다)


@dataclass
class Turn:
    stop: str                               # tool_calls · end · refusal · truncated · pause
    raw_stop: str                           # 공급자가 준 원래 값 (감사 기록용)
    model: str
    tool_calls: list = field(default_factory=list)
    text: str = ""
    usage: dict = field(default_factory=dict)


def _attr(obj, name, default=None):
    return getattr(obj, name, default) if obj is not None else default


class ClaudeBackend:
    provider = "anthropic"

    def __init__(self, client, model: str = CLAUDE_MODEL, *, system: str, effort: str = "medium",
                 use_fallbacks: bool = True):
        self.client, self.model, self.system = client, model, system
        self.effort, self.use_fallbacks = effort, use_fallbacks

    def tool_defs(self, mcp_tools, submit: dict) -> list:
        defs = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in mcp_tools]
        return defs + [{"name": submit["name"], "description": submit["description"], "strict": True,
                        "input_schema": submit["parameters"]}]

    def start(self, user_text: str) -> list:
        return [{"role": "user", "content": user_text}]

    async def next_turn(self, messages: list, tools: list) -> Turn:
        kw = dict(model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=self.system, tools=tools, messages=messages,
                  output_config={"effort": self.effort}, cache_control={"type": "ephemeral"})
        if self.use_fallbacks:            # 안전 분류기가 거절하면 서버가 다른 모델로 이어 받는다
            kw.update(betas=[FALLBACK_BETA], fallbacks="default")
        resp = await self.client.beta.messages.create(**kw)
        stop = {"tool_use": "tool_calls", "end_turn": "end", "stop_sequence": "end", "refusal": "refusal",
                "max_tokens": "truncated", "pause_turn": "pause"}.get(resp.stop_reason, "end")
        if stop not in ("refusal", "truncated"):
            messages.append({"role": "assistant", "content": resp.content})   # 받은 블록을 그대로 다시 보낸다
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if _attr(b, "type") == "tool_use"]
        text = "".join(_attr(b, "text", "") or "" for b in resp.content if _attr(b, "type") == "text")
        u = _attr(resp, "usage")
        usage = {k: _attr(u, k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                          "cache_creation_input_tokens") if _attr(u, k) is not None}
        if stop == "tool_calls" and not calls:
            stop = "end"
        return Turn(stop, resp.stop_reason, resp.model, calls, text, usage)

    def add_tool_results(self, messages: list, results: list) -> None:
        """results: [(tool_call_id, text, is_error)] → 한 user 메시지의 tool_result 블록들."""
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": text, "is_error": err} for cid, text, err in results]})

    def add_user_text(self, messages: list, text: str) -> None:
        messages.append({"role": "user", "content": text})


class OpenAIBackend:
    provider = "openai"

    def __init__(self, client, model: str = OPENAI_MODEL, *, system: str, effort: str = "medium"):
        self.client, self.model, self.system, self.effort = client, model, system, effort

    def tool_defs(self, mcp_tools, submit: dict) -> list:
        # MCP 도구 스키마에는 선택 인자(기본값)가 있어 strict를 끈다. submit_answer는 엄격 스키마라 strict로 받는다
        defs = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                  "parameters": t.input_schema, "strict": False}} for t in mcp_tools]
        return defs + [{"type": "function", "function": {"name": submit["name"], "description": submit["description"],
                                                         "parameters": submit["parameters"], "strict": True}}]

    def start(self, user_text: str) -> list:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": user_text}]

    async def next_turn(self, messages: list, tools: list) -> Turn:
        resp = await self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, parallel_tool_calls=True,
            reasoning_effort=self.effort, max_completion_tokens=MAX_OUTPUT_TOKENS)
        choice = resp.choices[0]
        msg, finish = choice.message, choice.finish_reason
        if _attr(msg, "refusal"):
            stop = "refusal"
        else:
            stop = {"tool_calls": "tool_calls", "stop": "end", "length": "truncated",
                    "content_filter": "refusal", "function_call": "tool_calls"}.get(finish, "end")
        raw_calls = [c for c in (_attr(msg, "tool_calls") or []) if _attr(c, "type", "function") == "function"]
        calls = []
        for c in raw_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("객체가 아님")
                calls.append(ToolCall(c.id, c.function.name, args))
            except ValueError as e:
                calls.append(ToolCall(c.id, c.function.name, {}, f"인자 JSON을 읽을 수 없음: {e}"))
        if stop not in ("refusal", "truncated"):
            entry = {"role": "assistant", "content": _attr(msg, "content")}
            if raw_calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in raw_calls]
            messages.append(entry)
        if stop == "end" and calls:
            stop = "tool_calls"
        u = _attr(resp, "usage")
        usage = {"input_tokens": _attr(u, "prompt_tokens"), "output_tokens": _attr(u, "completion_tokens")} if u else {}
        return Turn(stop, finish or "", resp.model, calls, _attr(msg, "content") or "",
                    {k: v for k, v in usage.items() if v is not None})

    def add_tool_results(self, messages: list, results: list) -> None:
        """호출마다 role=tool 메시지 하나. OpenAI에는 오류 표시 칸이 없어 본문 앞에 붙인다."""
        for cid, text, err in results:
            messages.append({"role": "tool", "tool_call_id": cid, "content": f"[오류] {text}" if err else text})

    def add_user_text(self, messages: list, text: str) -> None:
        messages.append({"role": "user", "content": text})


def make_backend(provider: str, *, model: Optional[str], system: str, client=None):
    """공급자 이름 → 어댑터. 인증키는 각 SDK가 환경변수(ANTHROPIC_API_KEY · OPENAI_API_KEY)에서 읽는다."""
    if provider == "anthropic":
        if client is None:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic()
        return ClaudeBackend(client, model or CLAUDE_MODEL, system=system)
    if provider == "openai":
        if client is None:
            from openai import AsyncOpenAI
            client = AsyncOpenAI()
        return OpenAIBackend(client, model or OPENAI_MODEL, system=system)
    raise ValueError(f"알 수 없는 공급자 {provider!r} (anthropic 또는 openai)")


def provider_errors(provider: str) -> tuple:
    """CLI가 잡을 (인증, 호출 한도, 상태 코드, 연결) 오류 클래스."""
    mod = __import__("anthropic" if provider == "anthropic" else "openai")
    return mod.AuthenticationError, mod.RateLimitError, mod.APIStatusError, mod.APIConnectionError
