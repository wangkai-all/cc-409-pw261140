"""把虚构场景载入正在运行的服务栈。

Usage: python3 -m recon.seed [--web http://127.0.0.1:8100]

Idempotent: re-running only reports duplicates.  Never touches real accounts.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def call(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {msg}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="载入虚构对账场景")
    ap.add_argument("--web", default="http://127.0.0.1:8100", help="网页/网关地址")
    ap.add_argument("--also-late", action="store_true", help="同时补入迟到流水（否则可由网页按钮触发）")
    ap.add_argument("--scenario", default=str(ROOT / "data" / "scenario.json"))
    args = ap.parse_args(argv)

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    for source in ("bank", "ledger"):
        res = call(args.web, "/api/ingest/batches", "POST",
                   {"source": source, "label": f"{source} 期初批次", "rows": scenario[source]})
        print(f"[seed] {source}: +{res['inserted_count']}，迟到 {res['late_count']}，重复 {len(res['duplicates'])}")
    if args.also_late:
        for source in ("bank", "ledger"):
            res = call(args.web, "/api/ingest/batches", "POST",
                       {"source": source, "label": "迟到补录", "rows": scenario["late"][source]})
            print(f"[seed] late {source}: +{res['inserted_count']}，迟到 {res['late_count']}")
    run = call(args.web, "/api/matcher/run", "POST", {"mode": "global", "scope": "seed"})
    s = run["summary"]
    print(f"[seed] 匹配完成：{s['selected']} 组，候选 {s['candidates_generated']}，耗时 {s['elapsed_ms']}ms")
    overview = call(args.web, "/api/balance/overview")
    print(f"[seed] 账务−银行 = {overview['raw_diff_cents'] / 100:.2f} 元；"
          f"校验余额 = {overview['identity_check_cents'] / 100:.2f} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
