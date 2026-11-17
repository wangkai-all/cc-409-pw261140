"""接入服务：接收银行流水与账务流水，负责去重、迟到识别与查询。

Independent process.  Other services only read through its HTTP API; nothing
else opens this SQLite file.
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta
from typing import Any

from .base import (
    App,
    AppError,
    DB,
    as_int,
    clean_text,
    new_id,
    now_iso,
    parse_date,
    query_one,
    serve_forever,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('bank', 'ledger')),
    label TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    late_count INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rows (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    source TEXT NOT NULL CHECK (source IN ('bank', 'ledger')),
    external_ref TEXT NOT NULL,
    account TEXT NOT NULL,
    value_date TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0),
    direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    counterparty TEXT,
    memo TEXT,
    currency TEXT NOT NULL DEFAULT 'CNY',
    late INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'void')),
    UNIQUE (source, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_rows_source_date ON rows(source, value_date);
CREATE INDEX IF NOT EXISTS idx_rows_account ON rows(source, account);
CREATE INDEX IF NOT EXISTS idx_rows_batch ON rows(batch_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DIRECTION_BY_SIGN = {1: "credit", -1: "debit"}


def _row_dict(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["amount_yuan"] = f"{r['amount_cents'] / 100:.2f}"
    out["late"] = bool(r["late"])
    return out


class IngestApp(App):
    service_name = "ingest"

    def __init__(self, db_path: str, *, late_grace_days: int = 2, verbose: bool = False):
        self.db = DB(db_path)
        self.db.init(SCHEMA)
        self.late_grace_days = late_grace_days
        super().__init__(verbose=verbose)

    # ------------------------------------------------------------------ #
    def register(self) -> None:
        r = self.router
        r.get(r"/health", lambda ctx: self.health_payload())
        r.get(r"/api/overview", self.overview)
        r.get(r"/api/rows", self.list_rows)
        r.post(r"/api/rows/lookup", self.rows_by_ids)
        r.get(r"/api/rows/(?P<row_id>[A-Za-z0-9_\-]+)", self.get_row)
        r.get(r"/api/rows/(?P<row_id>[A-Za-z0-9_\-]+)/peers", self.row_peers)
        r.get(r"/api/batches", self.list_batches)
        r.post(r"/api/batches", self.create_batch)
        r.get(r"/api/summary", self.summary)
        r.get(r"/api/internal/active-rows", self.active_rows)
        r.post(r"/api/internal/rows-by-ids", self.rows_by_ids)

    # ------------------------------------------------------------------ #
    def _source_or_422(self, value: Any) -> str:
        if value not in ("bank", "ledger"):
            raise AppError(422, "validation_error", "source 必须是 bank 或 ledger")
        return value

    def _normalize_row(self, raw: Any, idx: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AppError(422, "validation_error", f"第 {idx + 1} 行必须是对象")
        ref = clean_text(raw.get("external_ref"), f"rows[{idx}].external_ref", required=True, max_len=120)
        account = clean_text(raw.get("account"), f"rows[{idx}].account", required=True, max_len=80)
        value_date = parse_date(raw.get("value_date"), f"rows[{idx}].value_date")
        amount = as_int(raw.get("amount_cents"), f"rows[{idx}].amount_cents")
        if amount == 0:
            raise AppError(422, "validation_error", f"rows[{idx}].amount_cents 不能为 0")
        direction = raw.get("direction") or DIRECTION_BY_SIGN[1 if amount > 0 else -1]
        if direction not in ("debit", "credit"):
            raise AppError(422, "validation_error", f"rows[{idx}].direction 必须是 debit 或 credit")
        currency = (raw.get("currency") or "CNY").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise AppError(422, "validation_error", f"rows[{idx}].currency 必须是 3 位币种代码")
        posted_at = clean_text(raw.get("posted_at"), f"rows[{idx}].posted_at") or now_iso()
        return {
            "external_ref": ref,
            "account": account,
            "value_date": value_date.isoformat(),
            "posted_at": posted_at,
            "amount_cents": amount,
            "direction": direction,
            "counterparty": clean_text(raw.get("counterparty"), f"rows[{idx}].counterparty", max_len=120),
            "memo": clean_text(raw.get("memo"), f"rows[{idx}].memo", max_len=400),
            "currency": currency,
        }

    def _latitude_watermarks(self, conn, source: str) -> date | None:
        row = conn.execute(
            "SELECT MAX(value_date) AS d FROM rows WHERE source = ? AND status = 'active'", (source,)
        ).fetchone()
        return date.fromisoformat(row["d"]) if row["d"] else None

    def _insert_one(self, conn, batch_id: str, source: str, norm: dict[str, Any], watermark: date | None) -> tuple[dict | None, str]:
        exists = conn.execute(
            "SELECT id FROM rows WHERE source = ? AND external_ref = ?", (source, norm["external_ref"])
        ).fetchone()
        if exists:
            return None, exists["id"]
        vd = date.fromisoformat(norm["value_date"])
        late = 1 if watermark and vd < watermark - timedelta(days=self.late_grace_days) else 0
        row_id = new_id("row")
        conn.execute(
            """INSERT INTO rows (id, batch_id, source, external_ref, account, value_date, posted_at,
                   amount_cents, direction, counterparty, memo, currency, late, ingested_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
            (
                row_id, batch_id, source, norm["external_ref"], norm["account"], norm["value_date"],
                norm["posted_at"], norm["amount_cents"], norm["direction"], norm["counterparty"],
                norm["memo"], norm["currency"], late, now_iso(),
            ),
        )
        return {"id": row_id, "late": bool(late)}, ""

    def create_batch(self, ctx: dict[str, Any]) -> tuple[int, Any]:
        body = ctx["body"]
        if not isinstance(body, dict):
            raise AppError(422, "validation_error", "请求体必须是对象")
        source = self._source_or_422(body.get("source"))
        label = clean_text(body.get("label"), "label", max_len=120) or f"{source} 批次"
        note = clean_text(body.get("note"), "note", max_len=400)
        rows = body.get("rows")
        if not isinstance(rows, list) or not rows:
            raise AppError(422, "validation_error", "rows 必须是非空数组")
        if len(rows) > 5000:
            raise AppError(422, "validation_error", "单批最多 5000 行")
        norms = [self._normalize_row(raw, i) for i, raw in enumerate(rows)]
        seen: set[str] = set()
        batch_id = new_id("batch")
        inserted: list[dict] = []
        duplicates: list[dict] = []
        late_count = 0
        with self.db.tx() as conn:
            # Batch first: rows carry a foreign key to it.
            conn.execute(
                "INSERT INTO batches (id, source, label, note, created_at, row_count, late_count, duplicates) VALUES (?,?,?,?,?,0,0,0)",
                (batch_id, source, label, note, now_iso()),
            )
            # Latency is measured against the watermark that existed *before*
            # this batch.  Rows inside one batch never mark each other late.
            watermark = self._latitude_watermarks(conn, source)
            for norm in norms:
                if norm["external_ref"] in seen:
                    duplicates.append({"external_ref": norm["external_ref"], "reason": "本批次内重复"})
                    continue
                seen.add(norm["external_ref"])
                created, existing_id = self._insert_one(conn, batch_id, source, norm, watermark)
                if created:
                    created["external_ref"] = norm["external_ref"]
                    created["value_date"] = norm["value_date"]
                    inserted.append(created)
                    late_count += int(created["late"])
                else:
                    duplicates.append(
                        {"external_ref": norm["external_ref"], "reason": "已存在", "existing_id": existing_id}
                    )
            conn.execute(
                "UPDATE batches SET row_count=?, late_count=?, duplicates=? WHERE id=?",
                (len(inserted), late_count, len(duplicates), batch_id),
            )
        detail = []
        if inserted:
            detail = self.db.all(
                f"SELECT * FROM rows WHERE id IN ({','.join('?' * len(inserted))}) ORDER BY value_date",
                tuple(r["id"] for r in inserted),
            )
        return 201, {
            "batch": self._batch_dict(self.db.one("SELECT * FROM batches WHERE id = ?", (batch_id,))),
            "inserted": [_row_dict(r) for r in detail],
            "inserted_count": len(inserted),
            "late_count": late_count,
            "duplicates": duplicates,
        }

    @staticmethod
    def _batch_dict(b: dict[str, Any]) -> dict[str, Any]:
        return b

    # ------------------------------------------------------------------ #
    def overview(self, ctx: dict[str, Any]) -> Any:
        with self.db.read() as conn:
            totals = {
                src: dict(conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS total, "
                    "COALESCE(SUM(late),0) AS late FROM rows WHERE source = ? AND status='active'",
                    (src,),
                ).fetchone())
                for src in ("bank", "ledger")
            }
            accounts = [dict(r) for r in conn.execute(
                "SELECT source, account, COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS total "
                "FROM rows WHERE status='active' GROUP BY source, account ORDER BY source, account"
            ).fetchall()]
            batches = [dict(r) for r in conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT 20"
            ).fetchall()]
        return {"service": self.service_name, "totals": totals, "accounts": accounts, "batches": batches,
                "late_grace_days": self.late_grace_days}

    def _filters(self, ctx: dict[str, Any]) -> tuple[str, list[Any]]:
        q = ctx["query"]
        where = ["1=1"]
        params: list[Any] = []
        source = query_one(q, "source")
        if source:
            self._source_or_422(source)
            where.append("source = ?")
            params.append(source)
        account = query_one(q, "account")
        if account:
            where.append("account = ?")
            params.append(account)
        status = query_one(q, "status", "active")
        if status:
            if status not in ("active", "void", "all"):
                raise AppError(422, "validation_error", "status 必须是 active/void/all")
            if status != "all":
                where.append("status = ?")
                params.append(status)
        late = query_one(q, "late")
        if late in ("0", "1"):
            where.append("late = ?")
            params.append(int(late))
        date_from = query_one(q, "from")
        if date_from:
            where.append("value_date >= ?")
            params.append(parse_date(date_from, "from").isoformat())
        date_to = query_one(q, "to")
        if date_to:
            where.append("value_date <= ?")
            params.append(parse_date(date_to, "to").isoformat())
        text = query_one(q, "q")
        if text:
            where.append("(external_ref LIKE ? OR memo LIKE ? OR counterparty LIKE ?)")
            like = f"%{text}%"
            params.extend([like, like, like])
        return " AND ".join(where), params

    def list_rows(self, ctx: dict[str, Any]) -> Any:
        where, params = self._filters(ctx)
        limit = min(max(int(query_one(ctx["query"], "limit", "200") or 200), 1), 1000)
        offset = max(int(query_one(ctx["query"], "offset", "0") or 0), 0)
        order = "value_date DESC, id DESC" if query_one(ctx["query"], "sort") != "asc" else "value_date ASC, id ASC"
        rows = self.db.all(f"SELECT * FROM rows WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", tuple(params + [limit, offset]))
        count = self.db.one(f"SELECT COUNT(*) AS n FROM rows WHERE {where}", tuple(params))
        return {"rows": [_row_dict(r) for r in rows], "total": count["n"], "limit": limit, "offset": offset}

    def get_row(self, ctx: dict[str, Any]) -> Any:
        row = self.db.one("SELECT * FROM rows WHERE id = ?", (ctx["params"]["row_id"],))
        if not row:
            raise AppError(404, "row_not_found", "流水不存在")
        return _row_dict(row)

    def row_peers(self, ctx: dict[str, Any]) -> Any:
        """Rows on the *other* side that could be relevant (same account, near date)."""
        row = self.db.one("SELECT * FROM rows WHERE id = ?", (ctx["params"]["row_id"],))
        if not row:
            raise AppError(404, "row_not_found", "流水不存在")
        other = "ledger" if row["source"] == "bank" else "bank"
        rows = self.db.all(
            "SELECT * FROM rows WHERE source=? AND account=? AND status='active' "
            "AND value_date BETWEEN date(?, '-7 day') AND date(?, '+7 day') ORDER BY value_date LIMIT 200",
            (other, row["account"], row["value_date"], row["value_date"]),
        )
        return {"row": _row_dict(row), "peers": [_row_dict(r) for r in rows]}

    def list_batches(self, ctx: dict[str, Any]) -> Any:
        source = query_one(ctx["query"], "source")
        if source:
            self._source_or_422(source)
            batches = self.db.all("SELECT * FROM batches WHERE source=? ORDER BY created_at DESC LIMIT 100", (source,))
        else:
            batches = self.db.all("SELECT * FROM batches ORDER BY created_at DESC LIMIT 100")
        return {"batches": batches}

    def summary(self, ctx: dict[str, Any]) -> Any:
        where, params = self._filters(ctx)
        with self.db.read() as conn:
            totals = {
                src: dict(conn.execute(
                    f"SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS total FROM rows WHERE {where} AND source=?",
                    tuple(params + [src]),
                ).fetchone())
                for src in ("bank", "ledger")
            }
            daily = [dict(r) for r in conn.execute(
                f"SELECT value_date, source, COUNT(*) AS n, SUM(amount_cents) AS total FROM rows "
                f"WHERE {where} GROUP BY value_date, source ORDER BY value_date",
                tuple(params),
            ).fetchall()]
        return {"totals": totals, "daily": daily}

    # ------------------------------------------------------------------ #
    def active_rows(self, ctx: dict[str, Any]) -> Any:
        limit = min(max(int(query_one(ctx["query"], "limit", "5000") or 5000), 1), 20000)
        offset = max(int(query_one(ctx["query"], "offset", "0") or 0), 0)
        rows = self.db.all(
            "SELECT * FROM rows WHERE status='active' ORDER BY value_date, id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        total = self.db.one("SELECT COUNT(*) AS n FROM rows WHERE status='active'")["n"]
        return {"rows": [_row_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def rows_by_ids(self, ctx: dict[str, Any]) -> Any:
        body = ctx["body"] or {}
        ids = body.get("ids")
        if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(i, str) for i in ids):
            raise AppError(422, "validation_error", "ids 必须是字符串数组（最多 5000）")
        if not ids:
            return {"rows": []}
        marks = ",".join("?" * len(ids))
        rows = self.db.all(f"SELECT * FROM rows WHERE id IN ({marks})", tuple(ids))
        return {"rows": [_row_dict(r) for r in rows]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="对账接入服务")
    parser.add_argument("--host", default=os.environ.get("RECON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INGEST_PORT", "8101")))
    parser.add_argument("--db", default=os.environ.get("INGEST_DB", "./data/ingest.db"))
    parser.add_argument("--late-grace-days", type=int, default=int(os.environ.get("LATE_GRACE_DAYS", "2")))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    app = IngestApp(args.db, late_grace_days=args.late_grace_days, verbose=args.verbose)
    serve_forever(app, args.host, args.port, args.verbose)


if __name__ == "__main__":
    main()
