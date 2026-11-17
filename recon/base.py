"""Shared plumbing: JSON HTTP service, SQLite helpers, validation.

The three services are deliberately dependency free: only the Python standard
library is required.  Each service owns its own SQLite database and talks to
its peer over HTTP, so any one of them can be restarted on its own.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MAX_BODY = 2 * 1024 * 1024


# --------------------------------------------------------------------------- #
# time / ids / validation
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: Any, field: str = "value_date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AppError(422, "validation_error", f"{field} 必须是 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AppError(422, "validation_error", f"{field} 不是有效日期") from exc


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def clean_text(value: Any, field: str, *, required: bool = False, max_len: int = 240) -> str | None:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AppError(422, "validation_error", f"{field} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise AppError(422, "validation_error", f"{field} 不能为空")
    if len(value) > max_len:
        raise AppError(422, "validation_error", f"{field} 不能超过 {max_len} 个字符")
    return value or None


def as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(422, "validation_error", f"{field} 必须是整数（分）")
    return value


def query_one(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    vals = query.get(key)
    return vals[-1] if vals else default


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
class DB:
    """One tiny helper per service.  A connection is opened per transaction."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init(self, schema: str) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(schema)
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- #
# upstream HTTP client
# --------------------------------------------------------------------------- #
class UpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def http_json(
    url: str,
    method: str = "GET",
    body: Any = None,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
) -> Any:
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            parsed = raw.decode("utf-8", "replace")
        raise UpstreamError(f"upstream {method} {url} -> HTTP {exc.code}", exc.code, parsed) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpstreamError(f"无法连接上游 {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# HTTP app + router
# --------------------------------------------------------------------------- #
HandlerFn = Callable[[dict[str, Any]], Any]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], HandlerFn]] = []

    def add(self, method: str, pattern: str, fn: HandlerFn) -> None:
        self._routes.append((method.upper(), re.compile("^" + pattern + "$"), fn))

    def get(self, pattern: str, fn: HandlerFn) -> None:
        self.add("GET", pattern, fn)

    def post(self, pattern: str, fn: HandlerFn) -> None:
        self.add("POST", pattern, fn)

    def put(self, pattern: str, fn: HandlerFn) -> None:
        self.add("PUT", pattern, fn)

    def delete(self, pattern: str, fn: HandlerFn) -> None:
        self.add("DELETE", pattern, fn)

    def resolve(self, method: str, path: str):
        allowed = False
        for m, rx, fn in self._routes:
            match = rx.match(path)
            if match:
                if m == method:
                    return fn, match.groupdict()
                allowed = True
        if allowed:
            raise AppError(405, "method_not_allowed", f"{method} {path} 不支持")
        raise AppError(404, "not_found", f"未知路径 {path}")


class App:
    """Small router-backed application object."""

    service_name = "service"

    def __init__(self, *, verbose: bool = False):
        self.router = Router()
        self.verbose = verbose
        self.started_at = now_iso()
        self._started_mono = datetime.now(timezone.utc)
        self.register()

    def register(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def health_payload(self) -> dict[str, Any]:
        uptime = (datetime.now(timezone.utc) - self._started_mono).total_seconds()
        return {"ok": True, "service": self.service_name, "uptime_seconds": round(uptime, 1), "now": now_iso()}

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: Any):
        fn, params = self.router.resolve(method, path)
        ctx = {"params": params, "query": query, "body": body, "method": method, "path": path}
        result = fn(ctx)
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return 200, result


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ReconWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, AppError(413, "payload_too_large", "请求体过大").payload())
            return
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send(400, AppError(400, "invalid_json", "请求体不是合法 JSON").payload())
                return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            status, payload = self.server.app.dispatch(method, parsed.path, query, body)
        except AppError as exc:
            self._send(exc.status, exc.payload())
        except Exception as exc:  # pragma: no cover - defensive
            if getattr(self.server, "verbose", False):
                traceback.print_exc()
            self._send(500, AppError(500, "internal_error", f"服务内部错误: {exc}").payload())
        else:
            self._send(status, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self._send(204, {})


def make_server(app: App, host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), RequestHandler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    srv.verbose = verbose  # type: ignore[attr-defined]
    return srv


def serve_forever(app: App, host: str, port: int, verbose: bool = False) -> None:
    srv = make_server(app, host, port, verbose)
    actual = srv.server_address[1]
    print(f"[{app.service_name}] listening on http://{host}:{actual}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        shutdown = getattr(app, "shutdown", None)
        if callable(shutdown):
            shutdown()
