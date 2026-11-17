"""匹配服务：持有策略、生成匹配计划、执行人工确认/否决。

Confirmation semantics
----------------------
* ``confirmed``  (人工确认, effective)   — counts toward reconciliation.
* ``rejected``   (人工否决)              — never re-proposed automatically.
* ``proposed``   (自动提案, effective)   — recomputed on every strategy run.
* ``stale``      (确认已失效)            — a human confirmation whose evidence no
  longer passes the *current* strategy.  Kept as a record, not effective, and
  only a human may release it.  Automatic re-matching must not silently delete
  or re-point it.

The service keeps a snapshot of the last plan and only rewrites rows that are
not human-owned.  A global re-plan clears all auto matches; a local release only
re-plans rows affected by the released match.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from typing import Any

from .base import (
    App,
    AppError,
    DB,
    UpstreamError,
    clean_text,
    http_json,
    new_id,
    now_iso,
    query_one,
    serve_forever,
)
from .matching import (
    DEFAULT_STRATEGY,
    StrategyError,
    classify_kind,
    generate_candidates,
    normalize_strategy,
    score_group,
    solve_plan,
    strategy_hash,
    validate_group,
    validate_manual_group,
    validate_plan,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed','confirmed','rejected','stale','released')),
    effective INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL CHECK (origin IN ('auto','manual')),
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    score_parts TEXT NOT NULL DEFAULT '{}',
    reasons TEXT NOT NULL DEFAULT '[]',
    amount_delta_cents INTEGER NOT NULL DEFAULT 0,
    strategy_hash TEXT,
    run_id TEXT,
    note TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status, effective);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    strategy_hash TEXT NOT NULL,
    strategy TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulation_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _loads(text: str | None, default: Any) -> Any:
    import json

    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _match_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "effective": bool(row["effective"]),
        "origin": row["origin"],
        "bank_ids": _loads(row["bank_ids"], []),
        "ledger_ids": _loads(row["ledger_ids"], []),
        "score": row["score"],
        "score_parts": _loads(row["score_parts"], {}),
        "reasons": _loads(row["reasons"], []),
        "amount_delta_cents": row["amount_delta_cents"],
        "strategy_hash": row["strategy_hash"],
        "run_id": row["run_id"],
        "note": row["note"],
        "decided_by": row["decided_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class MatcherApp(App):
    service_name = "matcher"

    def __init__(self, db_path: str, ingest_url: str, *, verbose: bool = False):
        self.db = DB(db_path)
        self.db.init(SCHEMA)
        self.ingest_url = ingest_url.rstrip("/")
        self._outage_until = 0.0
        super().__init__(verbose=verbose)

    # Fault injection is explicit and demo-only: it never simulates data loss,
    # only a service outage / interrupted transaction.
    def dispatch(self, method: str, path: str, query, body):
        # During a drill the service reports itself as down on /health too, so
        # dependent services degrade instead of silently showing stale results.
        if time.time() < self._outage_until and not path.startswith("/api/simulate"):
            raise AppError(503, "matcher_outage", "匹配服务暂时不可用（演练中断中）")
        return super().dispatch(method, path, query, body)

    # ------------------------------------------------------------------ #
    def register(self) -> None:
        r = self.router
        r.get(r"/health", lambda ctx: self.health_payload())
        r.get(r"/api/strategy", self.get_strategy)
        r.put(r"/api/strategy", self.put_strategy)
        r.get(r"/api/matches", self.list_matches)
        r.get(r"/api/matches/(?P<match_id>[A-Za-z0-9_\-]+)", self.get_match)
        r.get(r"/api/candidates", self.candidates)
        r.post(r"/api/run", self.run_matching)
        r.get(r"/api/runs", self.list_runs)
        r.post(r"/api/matches/(?P<match_id>[A-Za-z0-9_\-]+)/confirm", self.confirm)
        r.post(r"/api/matches/(?P<match_id>[A-Za-z0-9_\-]+)/reject", self.reject)
        r.post(r"/api/matches/(?P<match_id>[A-Za-z0-9_\-]+)/release", self.release)
        r.post(r"/api/matches/(?P<match_id>[A-Za-z0-9_\-]+)/reinstate", self.reinstate)
        r.post(r"/api/manual", self.manual_create)
        r.post(r"/api/candidates/decide", self.decide_candidate)
        r.post(r"/api/simulate/interrupt", self.simulate_interrupt)
        r.post(r"/api/simulate/outage", self.simulate_outage)
        r.get(r"/api/simulate/events", self.simulation_events)
        r.get(r"/api/effective", self.effective)
        r.get(r"/api/internal/snapshot", self.snapshot)
        r.post(r"/api/internal/preview", self.preview)

    # ------------------------------------------------------------------ #
    # strategy
    # ------------------------------------------------------------------ #
    def get_strategy(self, ctx: dict[str, Any]) -> Any:
        raw = self.db.one("SELECT value, updated_at FROM config WHERE key='strategy'")
        strategy = _loads(raw["value"], None) if raw else normalize_strategy(None)
        return {
            "strategy": strategy,
            "hash": strategy_hash(strategy),
            "updated_at": raw["updated_at"] if raw else None,
            "default": DEFAULT_STRATEGY,
        }

    def put_strategy(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        raw = ctx["body"]
        if isinstance(raw, dict) and isinstance(raw.get("strategy"), dict):
            raw = raw["strategy"]
        try:
            strategy = normalize_strategy(raw)
        except StrategyError as exc:
            raise AppError(422, "invalid_strategy", str(exc))
        digest = strategy_hash(strategy)
        import json as _json

        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO config (key, value, updated_at) VALUES ('strategy', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (_json.dumps(strategy, ensure_ascii=False), now_iso()),
            )
            pending = conn.execute("SELECT COUNT(*) AS n FROM matches WHERE status='proposed'").fetchone()["n"]
        return 200, {
            "strategy": strategy,
            "hash": digest,
            "pending_proposals_require_rerun": pending,
            "hint": "策略已保存。需调用 POST /api/run 重算；人工确认不会自动失效，除非重新校验后证据不足。",
        }

    def _strategy(self) -> dict[str, Any]:
        raw = self.db.one("SELECT value FROM config WHERE key='strategy'")
        return _loads(raw["value"], None) if raw else normalize_strategy(None)

    # ------------------------------------------------------------------ #
    # upstream access
    # ------------------------------------------------------------------ #
    def _fetch_rows(self, page_size: int = 2000) -> tuple[list[dict], list[dict]]:
        rows: list[dict] = []
        offset = 0
        while True:
            try:
                data = http_json(f"{self.ingest_url}/api/internal/active-rows?limit={page_size}&offset={offset}")
            except UpstreamError as exc:
                raise AppError(503, "ingest_unavailable", f"接入服务不可用: {exc}")
            rows.extend(data["rows"])
            if len(rows) >= data["total"] or not data["rows"]:
                break
            offset += page_size
        bank = [r for r in rows if r["source"] == "bank"]
        ledger = [r for r in rows if r["source"] == "ledger"]
        return bank, ledger

    def _rows_by_id(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        try:
            data = http_json(f"{self.ingest_url}/api/internal/rows-by-ids", "POST", {"ids": ids})
        except UpstreamError as exc:
            raise AppError(503, "ingest_unavailable", f"接入服务不可用: {exc}")
        return {r["id"]: r for r in data["rows"]}

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def list_matches(self, ctx: dict[str, Any]) -> Any:
        q = ctx["query"]
        where, params = ["1=1"], []
        status = query_one(q, "status")
        if status:
            wanted = [s for s in status.split(",") if s]
            bad = [s for s in wanted if s not in ("proposed", "confirmed", "rejected", "stale", "released")]
            if bad:
                raise AppError(422, "validation_error", f"未知状态 {bad}")
            where.append(f"status IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        if query_one(q, "effective") in ("0", "1"):
            where.append("effective = ?")
            params.append(int(query_one(q, "effective")))
        row_id = query_one(q, "row_id")
        if row_id:
            where.append("(bank_ids LIKE ? OR ledger_ids LIKE ?)")
            params.extend([f'%"{row_id}"%', f'%"{row_id}"%'])
        origin = query_one(q, "origin")
        if origin:
            if origin not in ("auto", "manual"):
                raise AppError(422, "validation_error", "origin 必须是 auto/manual")
            where.append("origin = ?")
            params.append(origin)
        rows = self.db.all(
            f"SELECT * FROM matches WHERE {' AND '.join(where)} ORDER BY score DESC, created_at DESC LIMIT 500",
            tuple(params),
        )
        return {"matches": [_match_dict(r) for r in rows]}

    def get_match(self, ctx: dict[str, Any]) -> Any:
        row = self.db.one("SELECT * FROM matches WHERE id=?", (ctx["params"]["match_id"],))
        if not row:
            raise AppError(404, "match_not_found", "匹配不存在")
        match = _match_dict(row)
        rows = self._rows_by_id(match["bank_ids"] + match["ledger_ids"])
        match["bank_rows"] = [rows[i] for i in match["bank_ids"] if i in rows]
        match["ledger_rows"] = [rows[i] for i in match["ledger_ids"] if i in rows]
        match["missing_row_ids"] = [i for i in match["bank_ids"] + match["ledger_ids"] if i not in rows]
        return match

    def candidates(self, ctx: dict[str, Any]) -> Any:
        q = ctx["query"]
        bank, ledger = self._fetch_rows()
        strategy = self._strategy()
        cands = generate_candidates(bank, ledger, strategy)
        row_id = query_one(q, "row_id")
        if row_id:
            cands = [c for c in cands if row_id in c.bank_ids or row_id in c.ledger_ids]
        limit = min(max(int(query_one(q, "limit", "50") or 50), 1), 300)
        return {
            "strategy_hash": strategy_hash(strategy),
            "count": len(cands),
            "candidates": [c.as_dict() for c in cands[:limit]],
        }

    def list_runs(self, ctx: dict[str, Any]) -> Any:
        rows = self.db.all("SELECT * FROM runs ORDER BY created_at DESC LIMIT 50")
        return {"runs": [{**r, "strategy": _loads(r["strategy"], {}), "summary": _loads(r["summary"], {})} for r in rows]}

    # ------------------------------------------------------------------ #
    # matching runs
    # ------------------------------------------------------------------ #
    def _current_effective(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM matches WHERE effective=1 ORDER BY id")
        return [_match_dict(r) for r in rows]

    def _revalidate_human_matches(
        self, bank: list[dict], ledger: list[dict], strategy: dict, digest: str
    ) -> list[dict]:
        """Check confirmed matches against the current strategy.

        Rules (see the module docstring):

        * Hard violations (rows gone, sides mixed, amount beyond the manual hard
          limit) always invalidate -> ``stale``.
        * If the strategy is unchanged since the human confirmed, the override
          stays effective; only warnings are reported.
        * If the strategy changed and the group no longer satisfies the new
          rules, it is ``stale``: the confirmation is preserved as a record but
          excluded from reconciliation, and a human must re-affirm it.  This
          prevents an invalidated relation from silently distorting the
          unmatched items.

        Returns decisions without mutating anything so callers can preview.
        """
        by_id = {r["id"]: r for r in bank + ledger}
        out = []
        for row in self.db.all(
            "SELECT * FROM matches WHERE origin='manual' AND status IN ('confirmed','stale') ORDER BY id"
        ):
            m = _match_dict(row)
            if m["status"] == "stale":
                # A stale confirmation never re-enters the plan on its own.  We
                # only compute whether it *would* pass now, so the operator can
                # decide to re-confirm it explicitly.
                b = [by_id[i] for i in m["bank_ids"] if i in by_id]
                l = [by_id[i] for i in m["ledger_ids"] if i in by_id]
                missing = [i for i in m["bank_ids"] + m["ledger_ids"] if i not in by_id]
                hard = [] if missing else validate_manual_group(b, l, strategy)[0]
                out.append({
                    "match": m,
                    "problems": [f"流水已不可用: {', '.join(missing)}"] if missing else hard,
                    "warnings": [],
                    "same_strategy": False,
                    "stale": True,
                })
                continue
            missing = [i for i in m["bank_ids"] + m["ledger_ids"] if i not in by_id]
            problems: list[str] = []
            warnings: list[str] = []
            b = [by_id[i] for i in m["bank_ids"] if i in by_id]
            l = [by_id[i] for i in m["ledger_ids"] if i in by_id]
            same_strategy = m.get("strategy_hash") == digest
            if missing:
                problems = [f"流水已不可用: {', '.join(missing)}"]
            else:
                problems, hard_warnings = validate_manual_group(b, l, strategy)
                warnings = list(hard_warnings)
                score, parts, delta = score_group(b, l, bank + ledger, strategy)
                m["rescore"] = {"score": round(score, 2), "parts": parts, "delta": delta}
                if problems:
                    pass
                else:
                    rule_problems = validate_group(b, l, strategy)
                    if score < strategy["min_score"]:
                        rule_problems.append(
                            f"证据评分 {score:.1f} 低于当前自动阈值 {strategy['min_score']:.1f}"
                        )
                    if rule_problems:
                        if same_strategy:
                            warnings.extend(
                                f"{p}（确认时已接受的人工覆盖，保持有效）" for p in rule_problems
                            )
                        else:
                            problems = [
                                "策略已变更，人工确认在新策略下不再成立：" + "；".join(rule_problems)
                            ]
            out.append({"match": m, "problems": problems, "warnings": warnings,
                        "same_strategy": same_strategy})
        return out

    def run_matching(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        import time as _time

        started = _time.perf_counter()
        body = ctx["body"] or {}
        mode = body.get("mode") or "global"
        if mode not in ("global", "local", "preview"):
            raise AppError(422, "validation_error", "mode 必须是 global/local/preview")
        release_ids = body.get("release_match_ids") or []
        if not isinstance(release_ids, list) or not all(isinstance(i, str) for i in release_ids):
            raise AppError(422, "validation_error", "release_match_ids 必须是字符串数组")
        scope = clean_text(body.get("scope"), "scope", max_len=40) or mode

        bank, ledger = self._fetch_rows()
        strategy = self._strategy()
        digest = strategy_hash(strategy)

        revalidations = self._revalidate_human_matches(bank, ledger, strategy, digest)

        # effective manual matches that survive the new strategy form the locks
        locks: list[dict[str, Any]] = []
        stale_ids: list[str] = []
        for item in revalidations:
            m = item["match"]
            if item.get("stale"):
                continue  # already stale; awaiting an explicit re-confirmation
            if item["problems"]:
                stale_ids.append(m["id"])
            else:
                m["override_warnings"] = item["warnings"]
                locks.append(m)

        if mode == "preview":
            return 200, {
                "mode": "preview",
                "strategy_hash": digest,
                "would_stale": [
                    {"id": item["match"]["id"], "problems": item["problems"]}
                    for item in revalidations if item["problems"] and not item.get("stale")
                ],
                "already_stale": [item["match"]["id"] for item in revalidations if item.get("stale")],
                "locked_manual": [
                    {"id": m["id"], "score": m.get("rescore", {}).get("score")} for m in locks
                ],
                "hint": "预览不写入任何内容",
            }

        # Rows belonging to a stale confirmation stay reserved: the human's
        # decision still exists (just not effective), so automatic matching must
        # not quietly hand those rows to a different relation.  The operator
        # resolves it by re-confirming or releasing.
        stale_matches = [
            _match_dict(r) for r in self.db.all(
                "SELECT * FROM matches WHERE origin='manual' AND status='stale' ORDER BY id"
            )
        ]

        # local mode: restrict candidate pool to rows touched by released matches
        released_rows: set[str] = set()
        if mode == "local":
            if not release_ids:
                raise AppError(422, "validation_error", "局部重配必须提供 release_match_ids")
            for mid in release_ids:
                m = self.db.one("SELECT * FROM matches WHERE id=?", (mid,))
                if not m:
                    raise AppError(404, "match_not_found", f"匹配 {mid} 不存在")
                if m["status"] == "confirmed":
                    raise AppError(409, "manual_locked", f"匹配 {mid} 已人工确认，必须先显式释放")
                released_rows.update(_loads(m["bank_ids"], []) + _loads(m["ledger_ids"], []))

        locked_auto = 0
        if mode == "local":
            # Everything outside the released scope keeps its current automatic
            # relation: treat those groups as immovable for this solve.  This is
            # what makes a local re-plan cheap: only the affected rows compete.
            for row in self.db.all("SELECT * FROM matches WHERE origin='auto' AND effective=1"):
                m = _match_dict(row)
                if set(m["bank_ids"] + m["ledger_ids"]) & released_rows:
                    continue
                locks.append(m)
                locked_auto += 1

        reserved = [
            {"id": m["id"], "bank_ids": m["bank_ids"], "ledger_ids": m["ledger_ids"],
             "key": None, "score": m["score"], "reserved": "stale"}
            for m in stale_matches
        ]
        cands = generate_candidates(bank, ledger, strategy)
        if mode == "local":
            cands = [c for c in cands if set(c.bank_ids + c.ledger_ids) & released_rows]
        # A human rejection of this exact composition must survive re-runs.
        rejected_groups = {
            (tuple(sorted(_loads(r["bank_ids"], []))), tuple(sorted(_loads(r["ledger_ids"], []))))
            for r in self.db.all("SELECT bank_ids, ledger_ids FROM matches WHERE status='rejected' AND origin='manual'")
        }
        suppressed = [
            c for c in cands
            if (tuple(sorted(c.bank_ids)), tuple(sorted(c.ledger_ids))) in rejected_groups
        ]
        cands = [
            c for c in cands
            if (tuple(sorted(c.bank_ids)), tuple(sorted(c.ledger_ids))) not in rejected_groups
        ]
        plan = solve_plan(cands, locked=locks + reserved)
        selected = plan["selected"]
        elapsed_ms = round((_time.perf_counter() - started) * 1000, 1)
        fail_injected = getattr(self, "_fail_injected", None)
        self._fail_injected = None
        problems = validate_plan([
            {"id": "locked:" + str(i), "bank_ids": m["bank_ids"], "ledger_ids": m["ledger_ids"],
             "effective": True} for i, m in enumerate(locks)
        ] + [
            {"id": "sel:" + s["key"], "bank_ids": s["bank_ids"], "ledger_ids": s["ledger_ids"], "effective": True}
            for s in selected
        ])
        if problems:
            raise AppError(500, "plan_invariant_violation", "匹配计划违反唯一占用约束", problems)

        run_id = new_id("run")
        import json as _json

        sim_id = new_id("sim")
        if fail_injected is not None:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO simulation_events (id, kind, started_at, outcome, detail) "
                    "VALUES (?, 'interrupted_run', ?, 'started', ?)",
                    (sim_id, now_iso(), f"将在写入 {fail_injected} 组后中断（事务回滚）"),
                )
        try:
            with self.db.tx() as conn:
                removed_auto = 0
                if mode == "global":
                    # A global re-plan replaces every automatic relation.  Human
                    # decisions are never touched here (origin='manual').
                    cur = conn.execute("SELECT COUNT(*) AS n FROM matches WHERE origin='auto'").fetchone()["n"]
                    conn.execute("DELETE FROM matches WHERE origin='auto'")
                    removed_auto = cur
                else:
                    # Local release re-plan: only automatic matches touching the
                    # released rows are dropped; everything else stays untouched.
                    for row in conn.execute("SELECT * FROM matches WHERE origin='auto' AND effective=1").fetchall():
                        touched = set(_loads(row["bank_ids"], []) + _loads(row["ledger_ids"], []))
                        if touched & released_rows:
                            conn.execute("DELETE FROM matches WHERE id=?", (row["id"],))
                            removed_auto += 1
                for mid in stale_ids:
                    conn.execute(
                        "UPDATE matches SET status='stale', effective=0, updated_at=? WHERE id=? AND status='confirmed'",
                        (now_iso(), mid),
                    )
                if fail_injected is not None:
                    raise AppError(500, "simulated_interrupt",
                                   "演练：匹配写入过程中被中断，事务已回滚，旧匹配保持原样")
                auto_ids = []
                for sel in selected:
                    existing = conn.execute(
                        "SELECT id FROM matches WHERE origin='auto' AND effective=1 AND bank_ids=? AND ledger_ids=?",
                        (_json.dumps(sel["bank_ids"]), _json.dumps(sel["ledger_ids"])),
                    ).fetchone()
                    if existing:
                        auto_ids.append(existing["id"])
                        conn.execute("UPDATE matches SET strategy_hash=?, run_id=?, updated_at=? WHERE id=?",
                                     (digest, run_id, now_iso(), existing["id"]))
                        continue
                    mid = new_id("match")
                    conn.execute(
                        """INSERT INTO matches (id, kind, status, effective, origin, bank_ids, ledger_ids, score,
                               score_parts, reasons, amount_delta_cents, strategy_hash, run_id, created_at, updated_at)
                           VALUES (?,?, 'proposed', 1, 'auto', ?,?,?,?,?,?,?,?,?,?)""",
                        (mid, sel["kind"], _json.dumps(sel["bank_ids"]), _json.dumps(sel["ledger_ids"]),
                         sel["score"], _json.dumps(sel["parts"]), _json.dumps(sel["reasons"], ensure_ascii=False),
                         sel["amount_delta_cents"], digest, run_id, now_iso(), now_iso()),
                    )
                    auto_ids.append(mid)
                summary = {
                    "selected": len(selected),
                    "removed_auto": removed_auto,
                    "locked_manual": len(locks) - locked_auto,
                "locked_auto": locked_auto,
                "reserved_stale": len(reserved),
                    "stale_manual": len(stale_ids),
                    "blocked_by_locked": len(plan["blocked_by_locked"]),
                    "suppressed_rejected": len(suppressed),
                    "candidates_generated": len(cands) + len(suppressed),
                    "elapsed_ms": elapsed_ms,
                    "bank_rows": len(bank),
                    "ledger_rows": len(ledger),
                    "mode": mode,
                }
                conn.execute(
                    "INSERT INTO runs (id, strategy_hash, strategy, scope, created_at, summary) VALUES (?,?,?,?,?,?)",
                    (run_id, digest, _json.dumps(strategy, ensure_ascii=False), scope, now_iso(),
                     _json.dumps(summary, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT INTO outbox (topic, payload, created_at) VALUES ('matches.changed', ?, ?)",
                    (_json.dumps({"run_id": run_id, "mode": mode}, ensure_ascii=False), now_iso()),
                )

        except AppError:
            if fail_injected is not None:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE simulation_events SET finished_at=?, outcome='rolled_back', detail=? WHERE id=?",
                        (now_iso(), "写入过程中模拟中断：事务回滚，未留下部分匹配", sim_id),
                    )
            raise
        return 200, {
            "run_id": run_id,
            "strategy_hash": digest,
            "mode": mode,
            "summary": summary,
            "matches": [
                _match_dict(r) for r in self.db.all(
                    "SELECT * FROM matches WHERE run_id=? AND effective=1 ORDER BY score DESC", (run_id,)
                )
            ],
            "stale": [
                {"id": item["match"]["id"], "problems": item["problems"]}
                for item in revalidations if item["problems"] and not item.get("stale")
            ],
            "already_stale": [item["match"]["id"] for item in revalidations if item.get("stale")],
        }

    # ------------------------------------------------------------------ #
    # human decisions
    # ------------------------------------------------------------------ #
    def _touch(self, match_id: str) -> None:
        import json as _json

        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO outbox (topic, payload, created_at) VALUES ('matches.changed', ?, ?)",
                (_json.dumps({"match_id": match_id}, ensure_ascii=False), now_iso()),
            )

    def _get_raw(self, match_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM matches WHERE id=?", (match_id,))
        if not row:
            raise AppError(404, "match_not_found", "匹配不存在")
        return row

    def _occupancy(self, bank_ids: list[str], ledger_ids: list[str], exclude: str | None) -> list[dict[str, Any]]:
        """Effective matches that overlap the requested rows."""
        hits = []
        for row in self.db.all("SELECT * FROM matches WHERE effective=1 ORDER BY id"):
            if row["id"] == exclude:
                continue
            b = _loads(row["bank_ids"], [])
            l = _loads(row["ledger_ids"], [])
            overlap = sorted((set(b) & set(bank_ids)) | (set(l) & set(ledger_ids)))
            if overlap:
                hits.append({"match_id": row["id"], "status": row["status"], "origin": row["origin"],
                             "overlap_row_ids": overlap})
        return hits

    def _rows_for_match(self, m: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        ids = _loads(m["bank_ids"], []) + _loads(m["ledger_ids"], [])
        by_id = self._rows_by_id(ids)
        b = [by_id[i] for i in _loads(m["bank_ids"], []) if i in by_id]
        l = [by_id[i] for i in _loads(m["ledger_ids"], []) if i in by_id]
        return b, l

    def confirm(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        mid = ctx["params"]["match_id"]
        body = ctx["body"] or {}
        note = clean_text(body.get("note"), "note", max_len=400)
        actor = clean_text(body.get("actor"), "actor", max_len=80) or "操作员"
        row = self._get_raw(mid)
        m = _match_dict(row)
        if m["status"] == "rejected":
            raise AppError(409, "already_rejected", "该匹配已被人工否决，请先重新激活为候选")
        bank, ledger = self._rows_for_match(row)
        strategy = self._strategy()
        problems = validate_group(bank, ledger, strategy)
        if not problems:
            score, parts, delta = score_group(bank, ledger, bank + ledger, strategy)
            if score < strategy["min_score"]:
                problems = [f"证据评分 {score:.1f} 低于阈值 {strategy['min_score']:.1f}"]
        else:
            score = m["score"]
            parts = m["score_parts"]
        if problems:
            # A human can still take responsibility for an automatic proposal
            # whose evidence no longer passes the *current* strategy, but the
            # override must be explicit and is recorded as a warning.
            acknowledgements = body.get("acknowledge_warnings")
            if not acknowledgements:
                raise AppError(409, "evidence_insufficient",
                               "候选在当前策略下证据不足；如确认无误可带上 acknowledge_warnings 人工覆盖",
                               problems)
            bank_rows = bank
            ledger_rows = ledger
            if bank_rows and ledger_rows:
                hard, _ = validate_manual_group(bank_rows, ledger_rows, strategy)
                if hard:
                    raise AppError(409, "evidence_insufficient", "该匹配不满足人工确认的硬性条件", hard)
            warnings = list(problems)
        else:
            warnings = []
        conflicts = self._occupancy(m["bank_ids"], m["ledger_ids"], exclude=mid)
        if conflicts:
            raise AppError(409, "row_already_matched", "部分流水已被其他有效匹配占用", conflicts)
        import json as _json

        if warnings:
            merged = list(m["reasons"]) + ["人工覆盖：" + w for w in warnings]
        else:
            merged = list(m["reasons"])
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET status='confirmed', effective=1, origin='manual', note=COALESCE(?, note), "
                "decided_by=?, score=?, score_parts=?, reasons=?, strategy_hash=?, updated_at=? WHERE id=?",
                (note, actor, round(score, 2), _json.dumps(parts), _json.dumps(merged, ensure_ascii=False),
                 strategy_hash(strategy), now_iso(), mid),
            )
        self._touch(mid)
        return 200, self.get_match({"params": {"match_id": mid}})

    def reject(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        mid = ctx["params"]["match_id"]
        body = ctx["body"] or {}
        note = clean_text(body.get("note"), "note", max_len=400, required=True)
        actor = clean_text(body.get("actor"), "actor", max_len=80) or "操作员"
        row = self._get_raw(mid)
        if row["status"] == "confirmed":
            raise AppError(409, "manual_locked", "该匹配已人工确认；如需否定请先释放，再否决")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET status='rejected', effective=0, origin='manual', note=?, decided_by=?, updated_at=? WHERE id=?",
                (note, actor, now_iso(), mid),
            )
        self._touch(mid)
        return 200, _match_dict(self._get_raw(mid))

    def release(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        """Explicitly release a human match.  Never automatic.

        ``mode`` = ``local`` (default) drops only the automatic matches that
        overlap the released rows, so the caller can run a cheap local re-plan.
        ``global`` triggers a full re-plan, which is more expensive.
        ``none`` only releases and leaves re-matching to the operator.
        """
        mid = ctx["params"]["match_id"]
        body = ctx["body"] or {}
        actor = clean_text(body.get("actor"), "actor", max_len=80) or "操作员"
        note = clean_text(body.get("note"), "note", max_len=400)
        mode = body.get("mode") or "local"
        if mode not in ("local", "global", "none"):
            raise AppError(422, "validation_error", "mode 必须是 local/global/none")
        row = self._get_raw(mid)
        if row["origin"] != "manual" or row["status"] not in ("confirmed", "stale"):
            raise AppError(409, "not_manual", "只有人工确认或已失效的匹配才需要释放")
        ids = _loads(row["bank_ids"], []) + _loads(row["ledger_ids"], [])
        # A release is a distinct decision from a rejection: the record stays
        # for audit but the rows return to the pool.  It is never deleted and
        # never silently re-confirmed.
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE matches SET status='released', effective=0, decided_by=?, "
                "note=COALESCE(?, ?), updated_at=? WHERE id=?",
                (actor, note, f"人工释放（{actor}）", now_iso(), mid),
            )
        self._touch(mid)
        result: dict[str, Any] = {"released": _match_dict(self._get_raw(mid)), "replan": None}
        if mode == "global":
            _, data = self.run_matching({"body": {"mode": "global", "scope": "release-global"}})
            result["replan"] = data
        elif mode == "local":
            with self.db.tx() as conn:
                overlap = []
                for m in conn.execute("SELECT * FROM matches WHERE origin='auto' AND effective=1").fetchall():
                    touched = set(_loads(m["bank_ids"], []) + _loads(m["ledger_ids"], []))
                    if touched & set(ids):
                        overlap.append(m["id"])
                for oid in overlap:
                    conn.execute("DELETE FROM matches WHERE id=?", (oid,))
            result["replan"] = {
                "removed_auto_matches": overlap,
                "hint": "局部释放完成。调用 POST /api/run mode=local 并带上这些流水，可只重配受影响范围。",
                "affected_row_ids": ids,
            }
        return 200, result

    def reinstate(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        """Turn a rejected/stale/released match back into a reviewable candidate."""
        mid = ctx["params"]["match_id"]
        row = self._get_raw(mid)
        if row["status"] not in ("rejected", "stale", "released"):
            raise AppError(409, "not_reopenable", "只有已否决、已失效或已释放的匹配可以重新激活")
        with self.db.tx() as conn:
            # A reinstated human match is still human-owned but not effective;
            # a fresh confirm is needed before it counts again.
            conn.execute(
                "UPDATE matches SET status='proposed', effective=0, updated_at=? WHERE id=?", (now_iso(), mid)
            )
        self._touch(mid)
        return 200, _match_dict(self._get_raw(mid))

    def manual_create(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        body = ctx["body"] or {}
        bank_ids = body.get("bank_ids")
        ledger_ids = body.get("ledger_ids")
        for field, val in (("bank_ids", bank_ids), ("ledger_ids", ledger_ids)):
            if not isinstance(val, list) or not val or len(val) > 10 or not all(isinstance(i, str) for i in val):
                raise AppError(422, "validation_error", f"{field} 必须是 1..10 个字符串 ID")
        if len(set(bank_ids)) != len(bank_ids) or len(set(ledger_ids)) != len(ledger_ids):
            raise AppError(422, "validation_error", "同一侧不能重复引用流水")
        by_id = self._rows_by_id(bank_ids + ledger_ids)
        missing = [i for i in bank_ids + ledger_ids if i not in by_id]
        if missing:
            raise AppError(404, "row_not_found", "流水不存在", missing)
        b = [by_id[i] for i in bank_ids]
        l = [by_id[i] for i in ledger_ids]
        if any(r["source"] != "bank" for r in b) or any(r["source"] != "ledger" for r in l):
            raise AppError(422, "validation_error", "bank_ids 必须来自银行流水，ledger_ids 必须来自账务流水")
        conflicts = self._occupancy(bank_ids, ledger_ids, exclude=None)
        if conflicts:
            raise AppError(409, "row_already_matched", "部分流水已被其他有效匹配占用", conflicts)
        strategy = self._strategy()
        hard, warnings = validate_manual_group(b, l, strategy)
        score, parts, delta = score_group(b, l, b + l, strategy)
        kind = classify_kind(len(b), len(l))
        if kind is None:
            raise AppError(422, "unsupported_group", "只支持 1:1、1:N（拆单）、N:1（合单）")
        if score < strategy["min_score"]:
            warnings.append(f"证据评分 {score:.1f} 低于自动阈值 {strategy['min_score']:.1f}")
        if hard:
            raise AppError(409, "evidence_insufficient", "该组合不满足人工确认的硬性条件", hard)
        if body.get("confirm", True) and not body.get("acknowledge_warnings"):
            if warnings:
                raise AppError(409, "warnings_unacknowledged",
                               "该组合会覆盖自动规则，请确认已知悉这些警告", warnings)
        problems = hard
        mid = new_id("match")
        import json as _json

        with self.db.tx() as conn:
            conn.execute(
                """INSERT INTO matches (id, kind, status, effective, origin, bank_ids, ledger_ids, score,
                       score_parts, reasons, amount_delta_cents, strategy_hash, note, decided_by, created_at, updated_at)
                   VALUES (?,?,?,?, 'manual', ?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, kind, "confirmed" if body.get("confirm", True) else "proposed",
                 1 if body.get("confirm", True) else 0,
                 _json.dumps(bank_ids), _json.dumps(ledger_ids), round(score, 2), _json.dumps(parts),
                 _json.dumps([f"人工建立：{len(b)} 笔银行 ↔ {len(l)} 笔账务"] + warnings, ensure_ascii=False),
                 delta, strategy_hash(strategy), clean_text(body.get("note"), "note", max_len=400),
                 clean_text(body.get("actor"), "actor", max_len=80) or "操作员", now_iso(), now_iso()),
            )
        self._touch(mid)
        return 201, {**self.get_match({"params": {"match_id": mid}}), "warnings": warnings, "score": round(score, 2)}


    def decide_candidate(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        """Confirm or reject an ephemeral candidate by its row composition.

        Rejection records a ``rejected`` manual match so the exact composition
        is suppressed on later runs.  Confirmation is delegated to the same
        rules as manual creation (occupancy, hard limits, acknowledged warnings).
        """
        body = ctx["body"] or {}
        action = body.get("action")
        if action not in ("confirm", "reject"):
            raise AppError(422, "validation_error", "action 必须是 confirm 或 reject")
        bank_ids = body.get("bank_ids")
        ledger_ids = body.get("ledger_ids")
        if action == "confirm":
            # If the exact composition already exists as an automatic proposal,
            # confirming it must upgrade that record - not create a competing
            # one (which the occupancy guard would rightly refuse).
            if isinstance(bank_ids, list) and isinstance(ledger_ids, list):
                existing = self.db.one(
                    "SELECT * FROM matches WHERE bank_ids=? AND ledger_ids=? AND status IN ('proposed','stale')",
                    (__import__("json").dumps(bank_ids), __import__("json").dumps(ledger_ids)),
                )
                if existing:
                    return self.confirm({"params": {"match_id": existing["id"]}, "body": body})
            return self.manual_create(ctx)
        for field, val in (("bank_ids", bank_ids), ("ledger_ids", ledger_ids)):
            if not isinstance(val, list) or not val or len(val) > 10 or not all(isinstance(i, str) for i in val):
                raise AppError(422, "validation_error", f"{field} 必须是 1..10 个字符串 ID")
        by_id = self._rows_by_id(bank_ids + ledger_ids)
        missing = [i for i in bank_ids + ledger_ids if i not in by_id]
        if missing:
            raise AppError(404, "row_not_found", "流水不存在", missing)
        b = [by_id[i] for i in bank_ids]
        l = [by_id[i] for i in ledger_ids]
        if any(r["source"] != "bank" for r in b) or any(r["source"] != "ledger" for r in l):
            raise AppError(422, "validation_error", "bank_ids 必须来自银行流水，ledger_ids 必须来自账务流水")
        conflicts = self._occupancy(bank_ids, ledger_ids, exclude=None)
        if conflicts:
            raise AppError(409, "row_already_matched", "部分流水已被其他有效匹配占用，无法否决", conflicts)
        strategy = self._strategy()
        score, parts, delta = score_group(b, l, b + l, strategy)
        kind = classify_kind(len(b), len(l)) or "one_to_one"
        note = clean_text(body.get("note"), "note", max_len=400, required=True)
        actor = clean_text(body.get("actor"), "actor", max_len=80) or "操作员"
        import json as _json

        mid = new_id("match")
        with self.db.tx() as conn:
            conn.execute(
                """INSERT INTO matches (id, kind, status, effective, origin, bank_ids, ledger_ids, score,
                       score_parts, reasons, amount_delta_cents, strategy_hash, note, decided_by, created_at, updated_at)
                   VALUES (?,?, 'rejected', 0, 'manual', ?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, kind, _json.dumps(bank_ids), _json.dumps(ledger_ids), round(score, 2),
                 _json.dumps(parts), _json.dumps([f"人工否决：{note}"], ensure_ascii=False), delta,
                 strategy_hash(strategy), note, actor, now_iso(), now_iso()),
            )
        self._touch(mid)
        return 201, _match_dict(self._get_raw(mid))

    # ------------------------------------------------------------------ #
    # demo-only fault injection
    # ------------------------------------------------------------------ #
    def simulate_interrupt(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        body = ctx["body"] or {}
        fail_after = body.get("fail_after", 0)
        if isinstance(fail_after, bool) or not isinstance(fail_after, int) or not (0 <= fail_after <= 100):
            raise AppError(422, "validation_error", "fail_after 必须是 0..100 的整数")
        self._fail_injected = fail_after
        try:
            return self.run_matching({"body": {"mode": "global", "scope": "drill-interrupt"}})
        finally:
            self._fail_injected = None

    def simulate_outage(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        body = ctx["body"] or {}
        seconds = body.get("seconds", 5)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not (1 <= seconds <= 60):
            raise AppError(422, "validation_error", "seconds 必须是 1..60 的整数")
        self._outage_until = time.time() + seconds
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO simulation_events (id, kind, started_at, outcome, detail) VALUES (?, 'outage', ?, 'started', ?)",
                (new_id("sim"), now_iso(), f"{seconds} 秒内匹配 API 返回 503"),
            )
        return 200, {"outage_seconds": seconds, "until_unix": self._outage_until}

    def simulation_events(self, ctx: dict[str, Any]) -> Any:
        rows = self.db.all("SELECT * FROM simulation_events ORDER BY started_at DESC LIMIT 30")
        return {"events": rows}

    # ------------------------------------------------------------------ #
    # read models for balance service / web
    # ------------------------------------------------------------------ #
    def effective(self, ctx: dict[str, Any]) -> Any:
        rows = self.db.all("SELECT * FROM matches WHERE effective=1 ORDER BY score DESC")
        return {"matches": [_match_dict(r) for r in rows]}

    def snapshot(self, ctx: dict[str, Any]) -> Any:
        matches = self._current_effective()
        strategy = self._strategy()
        proposed = self.db.one("SELECT COUNT(*) AS n FROM matches WHERE status='proposed'")["n"]
        stale = self.db.one("SELECT COUNT(*) AS n FROM matches WHERE status='stale'")["n"]
        rejected = self.db.one("SELECT COUNT(*) AS n FROM matches WHERE status='rejected'")["n"]
        confirmed = self.db.one("SELECT COUNT(*) AS n FROM matches WHERE status='confirmed'")["n"]
        return {
            "strategy": strategy,
            "strategy_hash": strategy_hash(strategy),
            "matches": matches,
            "counts": {"effective": len(matches), "proposed": proposed, "stale": stale,
                       "rejected": rejected, "confirmed": confirmed},
            "generated_at": now_iso(),
        }

    def preview(self, ctx: dict[str, Any]) -> Any:
        """Read-only re-computation used by the balance service."""
        body = ctx["body"] or {}
        bank, ledger = self._fetch_rows()
        strategy = self._strategy()
        cands = generate_candidates(bank, ledger, strategy)
        locks = [m for m in self._current_effective() if m["origin"] == "manual"]
        plan = solve_plan(cands, locked=locks)
        matches = plan["selected"]
        return {
            "strategy_hash": strategy_hash(strategy),
            "matches": matches,
            "blocked_by_locked": len(plan["blocked_by_locked"]),
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="对账匹配计算器服务")
    parser.add_argument("--host", default=os.environ.get("RECON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MATCHER_PORT", "8102")))
    parser.add_argument("--db", default=os.environ.get("MATCHER_DB", "./data/matcher.db"))
    parser.add_argument("--ingest-url", default=os.environ.get("INGEST_URL", "http://127.0.0.1:8101"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    app = MatcherApp(args.db, args.ingest_url, verbose=args.verbose)
    serve_forever(app, args.host, args.port, args.verbose)


if __name__ == "__main__":
    main()
