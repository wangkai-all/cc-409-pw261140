"""在测试进程内启动三个服务（随机端口）+ 临时数据库。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from recon.balance import BalanceApp  # noqa: E402
from recon.base import make_server  # noqa: E402
from recon.ingest import IngestApp  # noqa: E402
from recon.matcher import MatcherApp  # noqa: E402


class Service:
    def __init__(self, app):
        self.server = make_server(app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Stack:
    """Three services wired to each other, plus a throwaway data directory."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="recon-test-")
        self.ingest = Service(IngestApp(os.path.join(self.tmp, "ingest.db"), late_grace_days=2))
        self.matcher = Service(MatcherApp(os.path.join(self.tmp, "matcher.db"), self.ingest.base))
        self.balance = Service(BalanceApp(os.path.join(self.tmp, "balance.db"), self.ingest.base, self.matcher.base))
        self.services = {"ingest": self.ingest, "matcher": self.matcher, "balance": self.balance}

    def close(self):
        for s in self.services.values():
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def call(self, service: str, path: str, method: str = "GET", body=None):
        url = self.services[service].base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"raw": raw.decode("utf-8", "replace")}

    def ok(self, service: str, path: str, method: str = "GET", body=None):
        status, data = self.call(service, path, method, body)
        if status >= 300:
            raise AssertionError(f"{method} {service}{path} -> {status}: {data}")
        return data

    def load_scenario(self, which: str = "base"):
        with open(os.path.join(ROOT, "data", "scenario.json"), encoding="utf-8") as fh:
            scenario = json.load(fh)
        rows = scenario if which == "base" else scenario["late"]
        for source in ("bank", "ledger"):
            self.ok("ingest", "/api/batches", "POST", {"source": source, "label": which, "rows": rows[source]})
        return rows

    def row_ids(self):
        data = self.ok("ingest", "/api/rows?limit=500&status=all")
        return {r["external_ref"]: r["id"] for r in data["rows"]}


class StackTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.addCleanup(self.stack.close)
