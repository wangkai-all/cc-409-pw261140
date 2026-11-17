"""启动三个服务 + 反向代理（开发/演示用）。

Usage:  python3 scripts/dev.py [--reset] [--seed] [--base-port 8101]

Each service runs in its own process so they can crash/restart independently;
``--reset`` deletes the local SQLite files first.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def wait_health(url: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.15)
    raise SystemExit(f"服务未就绪: {url} ({last})")


def seed(base: str) -> None:
    scenario = json.loads((DATA / "scenario.json").read_text(encoding="utf-8"))

    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    for source in ("bank", "ledger"):
        res = post("/api/ingest/batches", {"source": source, "label": f"{source} 期初批次", "rows": scenario[source]})
        print(f"  ingest {source}: +{res['inserted_count']} (late {res['late_count']}, dup {len(res['duplicates'])})", flush=True)
    res = post("/api/matcher/run", {"mode": "global", "scope": "seed"})
    print(f"  matcher: {res['summary']['selected']} 组匹配", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空 data/*.db")
    ap.add_argument("--seed", action="store_true", help="导入虚构流水并运行首次匹配")
    ap.add_argument("--base-port", type=int, default=int(os.environ.get("BASE_PORT", "8101")))
    ap.add_argument("--web-port", type=int, default=int(os.environ.get("WEB_PORT", "8100")))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.reset:
        for name in ("ingest.db", "matcher.db", "balance.db"):
            for suffix in ("", "-wal", "-shm"):
                p = DATA / (name + suffix)
                if p.exists():
                    p.unlink()
        print("数据已清空")

    ingest_port = args.base_port
    matcher_port = args.base_port + 1
    balance_port = args.base_port + 2
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(ROOT),
        INGEST_PORT=str(ingest_port),
        MATCHER_PORT=str(matcher_port),
        BALANCE_PORT=str(balance_port),
        INGEST_URL=f"http://127.0.0.1:{ingest_port}",
        MATCHER_URL=f"http://127.0.0.1:{matcher_port}",
        BALANCE_URL=f"http://127.0.0.1:{balance_port}",
        INGEST_DB=str(DATA / "ingest.db"),
        MATCHER_DB=str(DATA / "matcher.db"),
        BALANCE_DB=str(DATA / "balance.db"),
    )
    procs: list[subprocess.Popen] = []

    def spawn(name: str, module: str, extra: list[str] | None = None) -> None:
        cmd = [sys.executable, "-m", module] + (extra or [])
        if args.verbose:
            cmd.append("--verbose")
        procs.append(subprocess.Popen(cmd, cwd=str(ROOT), env=env))

    spawn("ingest", "recon.ingest")
    spawn("matcher", "recon.matcher")
    spawn("balance", "recon.balance")

    web_env = dict(env)
    web_env["WEB_PORT"] = str(args.web_port)
    procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / "web" / "server.py")],
        cwd=str(ROOT), env=web_env,
    ))

    try:
        wait_health(f"http://127.0.0.1:{ingest_port}/health")
        wait_health(f"http://127.0.0.1:{matcher_port}/health")
        wait_health(f"http://127.0.0.1:{balance_port}/health")
        wait_health(f"http://127.0.0.1:{args.web_port}/health")
        print(f"接入 http://127.0.0.1:{ingest_port}  匹配 http://127.0.0.1:{matcher_port}  "
              f"余额 http://127.0.0.1:{balance_port}")
        print(f"网页 http://127.0.0.1:{args.web_port}")
        if args.seed:
            seed(f"http://127.0.0.1:{args.web_port}")
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
