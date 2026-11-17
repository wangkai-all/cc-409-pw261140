"""余额分析端：只读消费接入与匹配结果，解释未达账项。

The service owns no authoritative data.  It reads rows from the ingest service
and effective matches from the matcher service over HTTP, then attributes the
raw balance difference to concrete causes:

    ledger_total - bank_total
      = sum(match amount deltas)          # differences accepted inside matches
      + unmatched ledger total            # ledger-only entries (未达/缺失)
      - unmatched bank total              # bank-only entries (未达/缺失)

The identity is verified on every run; a non-zero check means a match refers to
rows that are no longer available and is surfaced as a data problem instead of
being silently ignored.  If the matcher is down the service reports ``degraded``
and never fabricates a reconciled view.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

from .base import (
    App,
    AppError,
    DB,
    UpstreamError,
    http_json,
    now_iso,
    serve_forever,
)
from .matching import DEFAULT_STRATEGY

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy_hash TEXT,
    payload TEXT NOT NULL
);
"""


def _month(d: str) -> str:
    return d[:7]


class BalanceApp(App):
    service_name = "balance"

    def __init__(self, db_path: str, ingest_url: str, matcher_url: str, *, verbose: bool = False):
        self.db = DB(db_path)
        self.db.init(SCHEMA)
        self.ingest_url = ingest_url.rstrip("/")
        self.matcher_url = matcher_url.rstrip("/")
        super().__init__(verbose=verbose)

    def register(self) -> None:
        r = self.router
        r.get(r"/health", lambda ctx: self.health_payload())
        r.get(r"/api/overview", self.overview)
        r.get(r"/api/explain", self.explain)
        r.get(r"/api/snapshots", self.snapshots)
        r.get(r"/api/drift", self.drift)
        r.post(r"/api/snapshot", self.take_snapshot)

    # ------------------------------------------------------------------ #
    def _health(self) -> dict[str, Any]:
        out = {"ingest": False, "matcher": False}
        try:
            http_json(f"{self.ingest_url}/health", timeout=3)
            out["ingest"] = True
        except UpstreamError:
            pass
        try:
            http_json(f"{self.matcher_url}/health", timeout=3)
            out["matcher"] = True
        except UpstreamError:
            pass
        # /health returns 503 during a drill, so a plain call is enough here
        return out

    def _fetch_all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                data = http_json(
                    f"{self.ingest_url}/api/internal/active-rows?limit=5000&offset={offset}", timeout=15
                )
            except UpstreamError as exc:
                raise AppError(503, "degraded", f"接入服务不可用，无法计算余额分析（{exc}）")
            rows.extend(data["rows"])
            if not data["rows"] or len(rows) >= data["total"]:
                break
            offset += 5000
        return rows

    def _fetch(self) -> dict[str, Any]:
        health = self._health()
        if not health["ingest"]:
            raise AppError(503, "degraded", "接入服务不可用，无法计算余额分析")
        rows = self._fetch_all_rows()
        matches: list[dict[str, Any]] = []
        matcher_down = not health["matcher"]
        strategy_hash = None
        strategy = dict(DEFAULT_STRATEGY)
        if not matcher_down:
            try:
                snap = http_json(f"{self.matcher_url}/api/internal/snapshot", timeout=10)
                matches = snap.get("matches", [])
                strategy_hash = snap.get("strategy_hash")
                strategy = snap.get("strategy") or strategy
            except UpstreamError:
                matcher_down = True
        return {
            "rows": rows,
            "matches": matches,
            "strategy": strategy,
            "strategy_hash": strategy_hash,
            "matcher_down": matcher_down,
            "health": health,
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _signed(row: dict[str, Any]) -> int:
        return row["amount_cents"] if row.get("direction") == "credit" else -row["amount_cents"]

    def _classify_unmatched(
        self,
        row: dict[str, Any],
        opposite: list[dict[str, Any]],
        strategy: dict[str, Any],
        unmatched_ids: set[str] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Explain why this row is unmatched.

        * ``late``    — the row arrived after the reconciliation horizon (迟到流水)
        * ``amount``  — a plausible counterpart exists but the amount is off
        * ``standard``— no plausible counterpart at all (未达账项)
        """
        if row.get("late"):
            return "late", None
        window = int(strategy.get("day_window", 3))
        tol = int(strategy.get("amount_tolerance_cents", 100))
        sign = self._signed(row)
        best: dict[str, Any] | None = None
        for other in opposite:
            if unmatched_ids is not None and other["id"] not in unmatched_ids:
                continue
            if other["account"] != row["account"]:
                continue
            osign = self._signed(other)
            if (osign > 0) != (sign > 0):
                continue
            gap = abs((datetime.fromisoformat(other["value_date"]) - datetime.fromisoformat(row["value_date"])).days)
            if gap > window:
                continue
            delta = abs(osign - sign)
            if best is None or delta < best["delta_cents"] or (
                delta == best["delta_cents"] and gap < best["days"]
            ):
                best = {
                    "row_id": other["id"],
                    "external_ref": other.get("external_ref"),
                    "delta_cents": delta,
                    "days": gap,
                }
        # "amount" means: close enough in time that a human may want to look,
        # but outside the auto tolerance.  This is a hint, never a match.
        if best is not None and best["delta_cents"] <= max(tol * 50, abs(sign) // 5):
            return "amount", best
        return "standard", best

    def _explain(self, rows: list[dict[str, Any]], matches: list[dict[str, Any]], strategy: dict[str, Any]) -> dict[str, Any]:
        by_id = {r["id"]: r for r in rows}
        bank = [r for r in rows if r["source"] == "bank"]
        ledger = [r for r in rows if r["source"] == "ledger"]

        paired: set[str] = set()
        matched_delta = 0
        missing_refs: set[str] = set()
        for m in matches:
            ids = list(m.get("bank_ids", [])) + list(m.get("ledger_ids", []))
            for i in ids:
                if i in by_id:
                    paired.add(i)
                else:
                    missing_refs.add(i)
            if all(i in by_id for i in ids):
                matched_delta += int(m.get("amount_delta_cents", 0))

        bank_total = sum(self._signed(r) for r in bank)
        ledger_total = sum(self._signed(r) for r in ledger)
        unmatched = [r for r in rows if r["id"] not in paired]
        unmatched_bank = [r for r in unmatched if r["source"] == "bank"]
        unmatched_ledger = [r for r in unmatched if r["source"] == "ledger"]
        um_bank_total = sum(self._signed(r) for r in unmatched_bank)
        um_ledger_total = sum(self._signed(r) for r in unmatched_ledger)

        # classify & build period buckets
        period: dict[str, dict[str, Any]] = {}
        unmatched_ids = {r["id"] for r in unmatched}

        def bucket(month: str) -> dict[str, Any]:
            return period.setdefault(month, {
                "month": month,
                "late_cents": 0, "amount_cents": 0, "standard_cents": 0, "match_delta_cents": 0,
                "contribution_cents": 0,
                "counts": {"late": 0, "amount": 0, "standard": 0},
            })

        reasons: dict[str, dict[str, Any] | None] = {}
        for r in unmatched:
            opposite = ledger if r["source"] == "bank" else bank
            cls, hint = self._classify_unmatched(r, opposite, strategy, unmatched_ids)
            reasons[r["id"]] = hint
            b = bucket(_month(r["value_date"]))
            # Bucket amounts are contributions to (ledger - bank): a bank-only
            # row reduces the difference, a ledger-only row increases it.
            contribution = self._signed(r) if r["source"] == "ledger" else -self._signed(r)
            b[f"{cls}_cents"] += contribution
            b["counts"][cls] += 1
            b["contribution_cents"] = b.get("contribution_cents", 0) + contribution

        for m in matches:
            if int(m.get("amount_delta_cents", 0)) == 0:
                continue
            dates = [by_id[i]["value_date"] for i in m.get("bank_ids", []) + m.get("ledger_ids", []) if i in by_id]
            if dates:
                b = bucket(_month(min(dates)))
                b["match_delta_cents"] += int(m["amount_delta_cents"])
                b["contribution_cents"] = b.get("contribution_cents", 0) + int(m["amount_delta_cents"])

        attributed = matched_delta + um_ledger_total - um_bank_total
        raw_diff = ledger_total - bank_total
        check = raw_diff - attributed

        def _decorate(r: dict[str, Any], cls: str | None = None) -> dict[str, Any]:
            out = dict(r)
            out["amount_yuan"] = f"{self._signed(r) / 100:.2f}"
            hint = reasons.get(r["id"])
            if cls is None:
                cls, hint = self._classify_unmatched(
                    r, ledger if r["source"] == "bank" else bank, strategy, unmatched_ids
                )
            out["bucket"] = cls
            out["hint"] = hint
            return out

        bank_rows = sorted(unmatched_bank, key=lambda r: (r["value_date"], r["id"]))
        ledger_rows = sorted(unmatched_ledger, key=lambda r: (r["value_date"], r["id"]))

        return {
            "bank_total_cents": bank_total,
            "ledger_total_cents": ledger_total,
            "raw_diff_cents": raw_diff,
            "matched_delta_cents": matched_delta,
            "attributed_cents": attributed,
            "identity_check_cents": check,
            "missing_row_refs": sorted(missing_refs),
            "unmatched_bank": {
                "count": len(unmatched_bank),
                "total_cents": um_bank_total,
                "contribution_cents": -um_bank_total,
                "late_count": sum(1 for r in unmatched_bank if r.get("late")),
                "rows": [_decorate(r) for r in bank_rows[:500]],
            },
            "unmatched_ledger": {
                "count": len(unmatched_ledger),
                "total_cents": um_ledger_total,
                "contribution_cents": um_ledger_total,
                "late_count": sum(1 for r in unmatched_ledger if r.get("late")),
                "rows": [_decorate(r) for r in ledger_rows[:500]],
            },
            "matched": {"count": len(matches), "delta_cents": matched_delta},
            "periods": sorted(period.values(), key=lambda p: p["month"]),
            "period_contribution_total_cents": sum(p.get("contribution_cents", 0) for p in period.values()),
            "period_check_cents": sum(p.get("contribution_cents", 0) for p in period.values()) - raw_diff,
            "narrative": self._narrative(
                raw_diff, matched_delta, um_ledger_total, um_bank_total, len(matches),
                len(unmatched_bank), len(unmatched_ledger), check,
            ),
        }

    @staticmethod
    def _narrative(raw, matched_delta, um_ledger, um_bank, n_matches, n_ub, n_ul, check) -> list[str]:
        sign = lambda v: f"{v / 100:+,.2f}"  # noqa: E731
        lines = [
            f"账务合计 − 银行合计 = {sign(raw)} 元。",
            f"其中 {n_matches} 组有效匹配内部接受的金额差合计 {sign(matched_delta)} 元"
            f"（匹配本身不消除差额，只说明差额被哪一组流水解释）。",
            f"{n_ul} 笔仅见于账务的流水合计 {sign(um_ledger)} 元，"
            f"{n_ub} 笔仅见于银行的流水合计 {sign(um_bank)} 元。",
            f"验证：{sign(raw)} − ({sign(matched_delta)} + {sign(um_ledger)} − ({sign(um_bank)})) "
            f"= {sign(check)} 元。",
        ]
        if check != 0:
            lines.append("恒等式不为 0：存在指向已不可用流水的匹配，请检查数据一致性。")
        return lines

    def _accounts(self, rows: list[dict[str, Any]], matched_ids: set[str]) -> list[dict[str, Any]]:
        def _agg(rows_: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for r in rows_:
                b = out.setdefault(r["account"], {"count": 0, "total_cents": 0, "unmatched_count": 0,
                                                  "unmatched_cents": 0})
                signed = self._signed(r)
                b["count"] += 1
                b["total_cents"] += signed
                if r["id"] not in matched_ids:
                    b["unmatched_count"] += 1
                    b["unmatched_cents"] += signed
            return out

        b = _agg([r for r in rows if r["source"] == "bank"])
        l = _agg([r for r in rows if r["source"] == "ledger"])
        result = []
        for acc in sorted(set(b) | set(l)):
            bt = b.get(acc, {"count": 0, "total_cents": 0, "unmatched_count": 0, "unmatched_cents": 0})
            lt = l.get(acc, {"count": 0, "total_cents": 0, "unmatched_count": 0, "unmatched_cents": 0})
            result.append({
                "account": acc,
                "bank": bt,
                "ledger": lt,
                "diff_cents": lt["total_cents"] - bt["total_cents"],
            })
        return result

    def _compute(self, data: dict[str, Any]) -> dict[str, Any]:
        rows = data["rows"]
        matches = data["matches"]
        explanation = self._explain(rows, matches, data["strategy"])
        matched_ids = {i for m in matches for i in m.get("bank_ids", []) + m.get("ledger_ids", [])}
        return {
            "generated_at": now_iso(),
            "health": data["health"],
            "matcher_down": data["matcher_down"],
            "strategy_hash": data["strategy_hash"],
            "strategy": data["strategy"],
            "totals": {
                "bank_rows": sum(1 for r in rows if r["source"] == "bank"),
                "ledger_rows": sum(1 for r in rows if r["source"] == "ledger"),
                "effective_matches": len(matches),
                "span": {
                    "from": min((r["value_date"] for r in rows), default=None),
                    "to": max((r["value_date"] for r in rows), default=None),
                },
            },
            **explanation,
            "accounts": self._accounts(rows, matched_ids),
        }

    # ------------------------------------------------------------------ #
    def overview(self, ctx: dict[str, Any]) -> Any:
        return self._compute(self._fetch())

    def explain(self, ctx: dict[str, Any]) -> Any:
        return self._compute(self._fetch())

    def snapshots(self, ctx: dict[str, Any]) -> Any:
        rows = self.db.all("SELECT id, created_at, strategy_hash FROM snapshots ORDER BY created_at DESC LIMIT 20")
        return {"snapshots": rows}

    def take_snapshot(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        data = self._fetch()
        if data["matcher_down"]:
            raise AppError(409, "matcher_down", "匹配服务不可用，快照可能不完整；请先恢复匹配服务")
        result = self._compute(data)
        payload = {
            "diff_cents": result["raw_diff_cents"],
            "matched_delta_cents": result["matched_delta_cents"],
            "unmatched_bank_count": result["unmatched_bank"]["count"],
            "unmatched_ledger_count": result["unmatched_ledger"]["count"],
            "unmatched_bank_cents": result["unmatched_bank"]["total_cents"],
            "unmatched_ledger_cents": result["unmatched_ledger"]["total_cents"],
            "match_count": result["matched"]["count"],
            "identity_check_cents": result["identity_check_cents"],
            "period_check_cents": result["period_check_cents"],
        }
        snap_id = f"snap_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO snapshots (id, created_at, strategy_hash, payload) VALUES (?,?,?,?)",
                (snap_id, now_iso(), data["strategy_hash"], json.dumps(payload, ensure_ascii=False)),
            )
        return 201, {"id": snap_id, "created_at": now_iso(), "strategy_hash": data["strategy_hash"], **payload}

    def drift(self, ctx: dict[str, Any]) -> Any:
        data = self._fetch()
        result = self._compute(data)
        current = {
            "diff_cents": result["raw_diff_cents"],
            "matched_delta_cents": result["matched_delta_cents"],
            "unmatched_bank_count": result["unmatched_bank"]["count"],
            "unmatched_ledger_count": result["unmatched_ledger"]["count"],
            "unmatched_bank_cents": result["unmatched_bank"]["total_cents"],
            "unmatched_ledger_cents": result["unmatched_ledger"]["total_cents"],
            "match_count": result["matched"]["count"],
            "identity_check_cents": result["identity_check_cents"],
            "period_check_cents": result["period_check_cents"],
            "strategy_hash": data["strategy_hash"],
        }
        last = self.db.one("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 1")
        previous = None
        if last:
            previous = {"id": last["id"], "created_at": last["created_at"],
                        "strategy_hash": last["strategy_hash"], **json.loads(last["payload"])}
        changes = {}
        if previous:
            for key in ("diff_cents", "matched_delta_cents", "unmatched_bank_count", "unmatched_ledger_count",
                        "unmatched_bank_cents", "unmatched_ledger_cents", "match_count"):
                changes[key] = current[key] - previous.get(key, 0)
        return {
            "current": current,
            "previous": previous,
            "changes": changes,
            "strategy_changed": bool(previous and previous.get("strategy_hash") != current["strategy_hash"]),
            "matcher_down": data["matcher_down"],
            "health": data["health"],
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="余额分析服务")
    parser.add_argument("--host", default=os.environ.get("RECON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BALANCE_PORT", "8103")))
    parser.add_argument("--db", default=os.environ.get("BALANCE_DB", "./data/balance.db"))
    parser.add_argument("--ingest-url", default=os.environ.get("INGEST_URL", "http://127.0.0.1:8101"))
    parser.add_argument("--matcher-url", default=os.environ.get("MATCHER_URL", "http://127.0.0.1:8102"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    app = BalanceApp(args.db, args.ingest_url, args.matcher_url, verbose=args.verbose)
    serve_forever(app, args.host, args.port, args.verbose)


if __name__ == "__main__":
    main()
