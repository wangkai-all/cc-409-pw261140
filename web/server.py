"""网页服务：静态资源 + 同源反向代理到三个后端。

Same-origin proxy keeps the browser free of CORS handling and lets the page
show per-service health.  Service addresses are injected into /config.js so the
proxy routes can be changed without touching the front end.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web" / "static"

INGEST_URL = os.environ.get("INGEST_URL", "http://127.0.0.1:8101").rstrip("/")
MATCHER_URL = os.environ.get("MATCHER_URL", "http://127.0.0.1:8102").rstrip("/")
BALANCE_URL = os.environ.get("BALANCE_URL", "http://127.0.0.1:8103").rstrip("/")

PREFIXES = {
    "/api/ingest/": INGEST_URL,
    "/api/matcher/": MATCHER_URL,
    "/api/balance/": BALANCE_URL,
}
HEALTH = {"/health/ingest": INGEST_URL + "/health",
          "/health/matcher": MATCHER_URL + "/health",
          "/health/balance": BALANCE_URL + "/health"}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ReconWeb/1.0"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("WEB_VERBOSE"):
            sys.stderr.write("[web] %s\n" % (fmt % args))

    def _json(self, status: int, payload) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _proxy(self, method: str, url: str, body: bytes | None) -> None:
        headers = {"Accept": "application/json"}
        if body:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        except urllib.error.HTTPError as exc:
            data = exc.read()
            status = exc.code
            ctype = exc.headers.get("Content-Type", "application/json; charset=utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._json(503, {"error": {"code": "upstream_unavailable", "message": f"后端服务不可用: {exc}"}})
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _resolve(self) -> tuple[str, str] | None:
        parsed = urllib.parse.urlsplit(self.path)
        for prefix, base in PREFIXES.items():
            if parsed.path.startswith(prefix):
                target = f"{base}/api/{parsed.path[len(prefix):]}"
                if parsed.query:
                    target += "?" + parsed.query
                return target, parsed.path
        return None

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/config.js":
            body = (
                "window.RECON_CONFIG = " + json.dumps({
                    "ingest": "/api/ingest",
                    "matcher": "/api/matcher",
                    "balance": "/api/balance",
                }) + ";"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in HEALTH:
            try:
                with urllib.request.urlopen(HEALTH[parsed.path], timeout=3) as resp:
                    payload = json.loads(resp.read())
                self._json(200, {"ok": True, "upstream": payload})
            except urllib.error.HTTPError as exc:
                # A 503 from the service itself is "up but degraded" - report it
                # as such so the UI can distinguish drill outage from a crash.
                try:
                    payload = json.loads(exc.read())
                except Exception:  # noqa: BLE001
                    payload = None
                self._json(503, {"ok": False, "upstream": payload,
                                 "error": (payload or {}).get("error", {}).get("message", f"HTTP {exc.code}")})
            except Exception as exc:  # noqa: BLE001
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/health":
            self._json(200, {"ok": True, "service": "web"})
            return
        target = self._resolve()
        if target:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            self._proxy(method, target[0], body)
            return
        if method != "GET":
            self._json(405, {"error": {"code": "method_not_allowed", "message": "不支持的方法"}})
            return
        rel = parsed.path.lstrip("/") or "index.html"
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        path = (STATIC / rel).resolve()
        if not str(path).startswith(str(STATIC.resolve())) or not path.is_file():
            self._json(404, {"error": {"code": "not_found", "message": "文件不存在"}})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8100"))
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"[web] listening on http://{host}:{port}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
