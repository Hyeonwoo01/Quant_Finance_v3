"""산업·거시 공공 데이터 매일 수집 (관세청·한국은행 ECOS·미국 EIA). 설정: config/macro_series.json.

공급원마다 키가 있을 때만 돌고, 원본 응답(인증키 가림)과 PIT 레코드, 수집 기록을 남긴다. 한 공급원이 실패해도
다른 공급원은 계속한다. 인증키는 환경변수(DATA_GO_KR_KEY·ECOS_API_KEY·EIA_API_KEY)에서만 읽고 출력하지 않는다.

사용: python -m orchestration.macro_daily --data-dir data [--check | --probe]
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from collectors.base_client import BudgetExhausted, CallBudget, CollectorError
from collectors.macro_sources import (BOK_ORG_NAMES, SERIES_ECOS, SERIES_EIA, SERIES_KCS, EcosClient, EiaClient,
                                      KcsClient, SourceError, observation_records)
from core.repro import code_version
from core.source_registry import BOK_ECOS, EIA, KCS_ITEMTRADE, default_registry
from storage.manifests import BatchManifest
from storage.pit_store import PITFileStore
from storage.raw_store import RawStore

KST = ZoneInfo("Asia/Seoul")
CONFIG = Path(__file__).resolve().parent.parent / "config" / "macro_series.json"
KEY_ENV = {"kcs": "DATA_GO_KR_KEY", "ecos": "ECOS_API_KEY", "eia": "EIA_API_KEY"}


def _months_back(today: date, n: int) -> tuple[str, str]:
    y, m = today.year, today.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}{m:02d}", f"{today:%Y%m}"


class MacroRun:
    def __init__(self, data_dir, keys: dict, *, config: Optional[dict] = None, registry=None, max_calls: int = 300,
                 now=lambda: datetime.now(timezone.utc), version: Optional[str] = None, http_kw: Optional[dict] = None):
        self.data_dir = Path(data_dir)
        self.keys = {k: v for k, v in keys.items() if v}
        self.config = config or json.loads(CONFIG.read_text(encoding="utf-8"))
        self.registry = registry or default_registry()
        self.budget = CallBudget(max_calls)
        self.now, self.version, self.http_kw = now, version or code_version(), dict(http_kw or {})
        self.raw = RawStore(self.data_dir, self.registry)
        self.pit = PITFileStore(self.data_dir, self.registry)

    def _client(self, name):
        cls = {"kcs": KcsClient, "ecos": EcosClient, "eia": EiaClient}[name]
        return cls(self.keys[name], budget=self.budget, **self.http_kw)

    def _store(self, m: BatchManifest, contract, series_id: str, entity_id: str, provider: str, resp, obs, label: str,
               source_url: str):
        ref = self.raw.put(contract_id=contract.contract_id, source_id=contract.source_id, response=resp)
        store = self.pit.load(parts=[(series_id, entity_id)])
        recs, same = observation_records(obs, series_id=series_id, entity_id=entity_id, provider=provider,
                                         contract_id=contract.contract_id, raw_sha=ref.sha256,
                                         ingested_at_utc=self.now(), source_url=source_url, latest=store.latest)
        res = self.pit.append(recs, store=store)
        m.requests.append({"series": label, "params": resp.params, "http_status": resp.status, "sha256": ref.sha256,
                           "observations": len(obs), "added": res.added})
        m.count("added", res.added)
        m.count("unchanged", res.unchanged + same)
        m.count("series")
        if res.conflicts:
            m.warnings.append(f"{label}: 같은 revision_id인데 내용이 다름 {list(res.conflicts)[:5]}")

    def _guard(self, m: BatchManifest, label: str, fn) -> bool:
        """한 시리즈 실행. 인증 오류·예산 소진이면 False(그 공급원 중단)."""
        try:
            fn()
            return True
        except BudgetExhausted as e:
            m.errors.append(f"{label}: {e}")
            return False
        except SourceError as e:
            if e.no_data:
                m.warnings.append(f"{label}: 데이터 없음 ({e.code})")
                return True
            m.errors.append(f"{label}: {e}")
            return not e.auth
        except (CollectorError, ValueError, KeyError) as e:
            m.errors.append(f"{label}: {type(e).__name__}: {e}"[:300])
            return True

    def run_kcs(self, run_id: str) -> BatchManifest:
        cfg = self.config["kcs"]
        m = BatchManifest.start(run_id=run_id, contract=KCS_ITEMTRADE, code_version=self.version, now=self.now(),
                                target={"hs_codes": [h["code"] for h in cfg["hs_codes"]]})
        c = self._client("kcs")
        start, end = _months_back(self.now().astimezone(KST).date(), cfg.get("months_back", 14))
        for h in cfg["hs_codes"]:
            def one(h=h):
                resp, obs = c.itemtrade(h["code"], start, end)
                self._store(m, KCS_ITEMTRADE, SERIES_KCS, "KR", "KCS", resp, obs, f"HS{h['code']} {h['label']}",
                            "https://www.data.go.kr/data/15101609/openapi.do")
            if not self._guard(m, f"HS{h['code']}", one):
                break
        m.finish(self.now())
        m.write(self.data_dir)
        return m

    def run_ecos(self, run_id: str) -> BatchManifest:
        cfg = self.config["ecos"]
        m = BatchManifest.start(run_id=run_id, contract=BOK_ECOS, code_version=self.version, now=self.now(),
                                target={"series": [f"{s['stat']}/{s['item']}" for s in cfg["series"]]})
        c = self._client("ecos")
        today = self.now().astimezone(KST).date()
        orgs: dict = {}
        for s in cfg["series"]:
            def one(s=s):
                if s["stat"] not in orgs:
                    _, org, name = c.table_org(s["stat"])
                    orgs[s["stat"]] = org
                if orgs[s["stat"]] not in BOK_ORG_NAMES:
                    m.warnings.append(f"{s['stat']}: 통계작성기관이 '{orgs[s['stat']]}'라 받지 않음 (한국은행 작성 통계만, 약관 제5조)")
                    m.count("skipped_org")
                    return
                back = s.get("back", 24)
                if s["cycle"] == "D":
                    start, end = f"{today - timedelta(days=back):%Y%m%d}", f"{today:%Y%m%d}"
                else:
                    start, end = _months_back(today, back)
                resp, obs = c.search(s["stat"], s["cycle"], start, end, s["item"])
                self._store(m, BOK_ECOS, SERIES_ECOS, "KR", "BOK", resp, obs, f"{s['stat']}/{s['item']} {s['label']}",
                            "https://ecos.bok.or.kr/")
            if not self._guard(m, f"{s['stat']}/{s['item']}", one):
                break
        m.finish(self.now())
        m.write(self.data_dir)
        return m

    def run_eia(self, run_id: str) -> BatchManifest:
        cfg = self.config["eia"]
        m = BatchManifest.start(run_id=run_id, contract=EIA, code_version=self.version, now=self.now(),
                                target={"series": [s["series"] for s in cfg["series"]]})
        c = self._client("eia")
        start = f"{self.now().date() - timedelta(weeks=cfg.get('weeks_back', 26)):%Y-%m-%d}"
        for s in cfg["series"]:
            def one(s=s):
                resp, obs = c.series(s["route"], s["series"], s["frequency"], start)
                self._store(m, EIA, SERIES_EIA, "US", "EIA", resp, obs, f"{s['series']} {s['label']}",
                            f"https://www.eia.gov/opendata/browser/{s['route']}")
            if not self._guard(m, s["series"], one):
                break
        m.finish(self.now())
        m.write(self.data_dir)
        return m

    def run(self) -> dict:
        run_id = f"{self.now():%Y%m%dT%H%M%SZ}"
        out = {}
        for name, fn in (("kcs", self.run_kcs), ("ecos", self.run_ecos), ("eia", self.run_eia)):
            out[name] = fn(run_id) if name in self.keys else None
        return out

    # ---------- 점검 ----------
    def check(self) -> list[str]:
        """저장 없이 공급원마다 작은 호출 하나로 인증키·연결을 확인한다."""
        lines, today = [], self.now().astimezone(KST).date()
        tests = {
            "kcs": lambda c: f"관측 {len(c.itemtrade('8542', *_months_back(today, 2))[1])}개",
            "ecos": lambda c: "기준금리 통계표 작성기관 %r" % (c.table_org("722Y001")[1],),
            "eia": lambda c: f"관측 {len(c.series('petroleum/stoc/wstk', 'WCESTUS1', 'weekly', f'{today - timedelta(days=21)}', 5)[1])}개",
        }
        for name, env in KEY_ENV.items():
            if name not in self.keys:
                lines.append(f"{name}: {env} 없음")
                continue
            try:
                lines.append(f"{name}: 정상 ({tests[name](self._client(name))})")
            except (CollectorError, ValueError) as e:
                lines.append(f"{name}: 실패 — {e}")
        return lines

    def probe(self) -> list[str]:
        """응답 구조 확인용 (저장 없음): 관세청 첫 행 필드, ECOS 품목 코드 후보, EIA 첫 행 필드."""
        lines, today = [], self.now().astimezone(KST).date()
        if "kcs" in self.keys:
            try:
                c = self._client("kcs")
                r, obs = c.itemtrade("8542", *_months_back(today, 3))
                lines.append(f"[kcs] HTTP {r.status}, 관측 {len(obs)}개, 앞부분: {obs[:4]}")
                lines.append("[kcs] 원문 앞부분: " + c.http.scrub(r.body[:600].decode("utf-8", "replace")).replace("\n", " "))
            except (CollectorError, ValueError) as e:
                lines.append(f"[kcs] 실패 — {e}")
        if "ecos" in self.keys:
            c = self._client("ecos")
            for stat in self.config["ecos"].get("probe_tables", []):
                try:
                    _, org, name = c.table_org(stat)
                    _, rows = c.items(stat, 1, 1000)
                    lines.append(f"[ecos] {stat} {name} 작성기관 {org!r} 품목 {len(rows)}행")
                    kws = self.config["ecos"].get("probe_keywords", [])
                    for row in rows:
                        if row.get("CYCLE") == "M" and any(k in (row.get("ITEM_NAME") or "") for k in kws):
                            lines.append(f"   {row['ITEM_CODE']} (상위 {row.get('P_ITEM_CODE')}) {row['ITEM_NAME']} "
                                         f"{row.get('START_TIME')}~{row.get('END_TIME')} {row.get('UNIT_NAME')}")
                except (CollectorError, ValueError) as e:
                    lines.append(f"[ecos] {stat} 실패 — {e}")
        if "eia" in self.keys:
            c = self._client("eia")
            for s in self.config["eia"]["series"]:
                try:
                    r, obs = c.series(s["route"], s["series"], s["frequency"], f"{today - timedelta(days=30)}", 3)
                    lines.append(f"[eia] {s['series']} HTTP {r.status}, 관측 {len(obs)}개: {obs[:2]}")
                except (CollectorError, ValueError) as e:
                    lines.append(f"[eia] {s['series']} 실패 — {e}")
        return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=os.environ.get("QF_DATA_DIR", "data"))
    ap.add_argument("--check", action="store_true", help="저장 없이 인증키·연결만 확인")
    ap.add_argument("--probe", action="store_true", help="저장 없이 응답 구조와 품목 코드 후보 출력")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    keys = {name: os.environ.get(env, "").strip() for name, env in KEY_ENV.items()}
    if not any(keys.values()):
        print("인증키가 하나도 없음 (DATA_GO_KR_KEY·ECOS_API_KEY·EIA_API_KEY)", file=sys.stderr)
        return 2
    run = MacroRun(args.data_dir, keys)
    if args.check or args.probe:
        lines = run.check() if args.check else run.probe()
        print("\n".join(lines))
        return 1 if any("실패" in ln for ln in lines) else 0
    res = run.run()
    failed = False
    for name, m in res.items():
        if m is None:
            print(f"{name}: {KEY_ENV[name]} 없음 — 건너뜀")
            continue
        print(f"{name}: {m.status} {m.counts}")
        for w in m.warnings[:10]:
            print(f"  경고: {w}")
        for e in m.errors[:10]:
            print(f"  오류: {e}")
        failed |= m.status == "failed"
    print(f"호출 {run.budget.used}회")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
