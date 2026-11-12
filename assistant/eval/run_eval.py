"""어시스턴트 평가 세트 실행 (가이드 14-4). 실제 모델을 부르므로 API 비용이 든다.

    ANTHROPIC_API_KEY=... python -m assistant.eval.run_eval --data-dir data [--only id1,id2] [--out report.json]

질문마다 기준 시각(as_of)이 고정돼 있어, data 브랜치가 계속 자라도 같은 정보로 답한다 (PIT 재현성).
재는 것:
- status_ok: 답했어야 할 질문에 답하고, 보류해야 할 질문(이용 조건상 막힌 데이터·시점 밖·미래)에 보류했는가
- tools_ok: 기대한 도구를 썼는가 (도구 선택)
- evidence_ok: 기대한 근거(evidence_id 접두사)를 단 주장이 있는가 (출처 연결)
- verified_rate: 주장 중 VERIFIED 비율 (수치·의미 일치)
- unclaimed: 근거 없이 쓴 숫자 수
- as_of_overrides: 모델이 기준 시각을 바꾸려 한 횟수 (Agent가 막음)
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from assistant.agent import MODEL, ask

GOLDEN = Path(__file__).with_name("golden.jsonl")


def score(case: dict, res) -> dict:
    sub, ver = res.submission or {}, res.verification or {}
    used = [c["name"] for s in res.steps for c in s["tool_calls"]]
    claims = ver.get("claims", [])
    answered = sub.get("status") == "ANSWERED"
    status_ok = (sub.get("status") == case["expect_status"]) and res.status != "FAILED"
    prefix = case.get("expect_evidence_prefix")
    return {
        "id": case["id"], "category": case["category"], "result": res.status, "submitted": sub.get("status"),
        "status_ok": status_ok,
        "tools_ok": all(t in used for t in case["expect_tools"]),
        "evidence_ok": (not prefix) or any((c.get("evidence_id") or "").startswith(prefix) for c in claims),
        "claims": len(claims),
        "verified_rate": (sum(c["status"] == "VERIFIED" for c in claims) / len(claims)) if claims else None,
        "unclaimed": len(ver.get("unclaimed_numbers", [])) if answered else 0,
        "as_of_overrides": sum(1 for s in res.steps for c in s["tool_calls"] if c.get("overrides")),
        "tools": used, "steps": len(res.steps),
        "tokens_in": sum(s["usage"].get("input_tokens", 0) for s in res.steps),
        "tokens_out": sum(s["usage"].get("output_tokens", 0) for s in res.steps),
        "error": res.error,
    }


def summary_md(rows: list, model: str) -> str:
    n = len(rows)
    pct = lambda k: f"{sum(r[k] for r in rows)}/{n}"
    rates = [r["verified_rate"] for r in rows if r["verified_rate"] is not None]
    lines = [f"## 어시스턴트 평가 ({model})", "",
             f"- 답·보류 판단 맞음: {pct('status_ok')} · 도구 선택 맞음: {pct('tools_ok')} · 근거 연결 맞음: {pct('evidence_ok')}",
             f"- 주장 VERIFIED 비율(평균): {sum(rates) / len(rates):.0%}" if rates else "- 주장 없음",
             f"- 근거 없는 숫자: {sum(r['unclaimed'] for r in rows)}개 · 기준 시각 변경 시도(차단됨): {sum(r['as_of_overrides'] for r in rows)}회",
             f"- 토큰: 입력 {sum(r['tokens_in'] for r in rows):,} · 출력 {sum(r['tokens_out'] for r in rows):,}", "",
             "| 질문 | 분류 | 결과 | 답/보류 | 도구 | 근거 | VERIFIED | 단계 |", "|---|---|---|---|---|---|---|---|"]
    ok = lambda b: "✅" if b else "❌"
    for r in rows:
        vr = "—" if r["verified_rate"] is None else f"{r['verified_rate']:.0%}"
        lines.append(f"| {r['id']} | {r['category']} | {r['result']} | {ok(r['status_ok'])} {r['submitted'] or ''} | "
                     f"{ok(r['tools_ok'])} | {ok(r['evidence_ok'])} | {vr} | {r['steps']} |")
    return "\n".join(lines)


async def main_async(args) -> int:
    cases = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only.split(","))]
    rows, transcripts = [], []
    for c in cases:
        res = await ask(c["question"], data_dir=args.data_dir, as_of=c["as_of"], model=args.model)
        rows.append(score(c, res))
        transcripts.append({"case": c, "result": res.to_dict()})
        print(f"[{c['id']}] {res.status}\n{res.rendered}\n", flush=True)
    md = summary_md(rows, args.model)
    print(md)
    if args.out:
        Path(args.out).write_text(json.dumps({"model": args.model, "rows": rows, "transcripts": transcripts},
                                             ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--only", help="쉼표로 구분한 질문 id만")
    ap.add_argument("--out", help="전체 기록 JSON 경로")
    ap.add_argument("--summary", help="요약 마크다운을 덧붙일 파일 (예: $GITHUB_STEP_SUMMARY)")
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
