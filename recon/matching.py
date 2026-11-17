"""匹配计算器：确定性、可解释的候选生成与匹配计划求解。

The engine is pure Python and has no HTTP/database dependency.  Given two sets
of rows and a strategy it produces:

* candidate groups (1:1, split 1 bank -> n ledger, merge n bank -> 1 ledger),
  each with an explainable score breakdown;
* a matching plan: a selection of non-overlapping candidate groups covering as
  many rows as possible, preferring higher scores.

Invariants enforced by :func:`validate_plan`:

1. A row may belong to at most one *effective* match.
2. A manually confirmed match is never dropped or re-pointed silently; if a
   strategy change makes it invalid it becomes ``stale`` (confirmation kept,
   no longer effective) and must be explicitly released by a human.
3. Every plan is deterministic for the same input + strategy.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
DEFAULT_STRATEGY: dict[str, Any] = {
    "name": "标准策略",
    "day_window": 3,
    "amount_tolerance_cents": 100,
    "min_score": 55.0,
    "allow_split": True,
    "allow_merge": True,
    "max_parts": 3,
    "score_weights": {
        "amount": 55.0,
        "date": 20.0,
        "reference": 15.0,
        "account": 10.0,
        "counterparty": 10.0,
    },
    "split_penalty": 4.0,
    "merge_penalty": 4.0,
}

SIGN = {"debit": -1, "credit": 1}

# A human may confirm a group that the automatic rules would not propose, but
# only within hard limits.  Beyond these the group is not a reconciliation
# relation at all (wrong side, wildly different amount, unrelated period).
MANUAL_LIMITS: dict[str, int] = {
    "amount_cents": 5000,     # 50 元 absolute amount difference
    "days": 31,               # one month between the two sides
    "parts": 10,              # rows on the many-side of a split/merge
}


def normalize_strategy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a fully populated strategy, rejecting nonsense values."""
    strategy = copy.deepcopy(DEFAULT_STRATEGY)
    if raw is None:
        return strategy
    if not isinstance(raw, dict):
        raise StrategyError("策略必须是对象")
    errors: list[str] = []

    def take_int(key: str, lo: int, hi: int) -> None:
        if key in raw:
            val = raw[key]
            if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                errors.append(f"{key} 必须是 {lo}..{hi} 的整数")
            else:
                strategy[key] = val

    def take_num(key: str, lo: float, hi: float) -> None:
        if key in raw:
            val = raw[key]
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not (lo <= float(val) <= hi):
                errors.append(f"{key} 必须是 {lo}..{hi} 的数字")
            else:
                strategy[key] = float(val)

    if "name" in raw:
        val = raw["name"]
        if not isinstance(val, str) or not val.strip() or len(val) > 60:
            errors.append("name 必须是非空字符串（≤60 字符）")
        else:
            strategy["name"] = val.strip()
    take_int("day_window", 0, 30)
    take_int("amount_tolerance_cents", 0, 100000)
    take_num("min_score", 0.0, 100.0)
    take_int("max_parts", 2, 10)
    take_num("split_penalty", 0.0, 100.0)
    take_num("merge_penalty", 0.0, 100.0)
    for key in ("allow_split", "allow_merge"):
        if key in raw:
            if not isinstance(raw[key], bool):
                errors.append(f"{key} 必须是布尔值")
            else:
                strategy[key] = raw[key]
    if "score_weights" in raw:
        weights = raw["score_weights"]
        if not isinstance(weights, dict):
            errors.append("score_weights 必须是对象")
        else:
            for key, val in weights.items():
                if key not in strategy["score_weights"]:
                    errors.append(f"未知评分项 {key}")
                elif isinstance(val, bool) or not isinstance(val, (int, float)) or not (0 <= float(val) <= 100):
                    errors.append(f"score_weights.{key} 必须是 0..100 的数字")
                else:
                    strategy["score_weights"][key] = float(val)
    if errors:
        raise StrategyError("；".join(errors))
    return strategy


def strategy_hash(strategy: dict[str, Any]) -> str:
    canonical = json.dumps(strategy, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class StrategyError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# candidate generation
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    key: str
    kind: str                     # one_to_one | split | merge
    bank_ids: list[str]
    ledger_ids: list[str]
    amount_delta_cents: int       # ledger_sum - bank_sum
    score: float
    reasons: list[str]
    parts: dict[str, float] = field(default_factory=dict)
    auto_eligible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "bank_ids": self.bank_ids,
            "ledger_ids": self.ledger_ids,
            "amount_delta_cents": self.amount_delta_cents,
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "parts": {k: round(v, 2) for k, v in self.parts.items()},
            "auto_eligible": self.auto_eligible,
        }


def _row_date(row: dict[str, Any]) -> date:
    return date.fromisoformat(row["value_date"])


def _signed(row: dict[str, Any]) -> int:
    return row["amount_cents"] if row.get("direction") == "credit" else -row["amount_cents"]


def _ref_tokens(ref: str) -> set[str]:
    import re

    return {t for t in re.split(r"[^A-Za-z0-9]+", (ref or "").upper()) if len(t) >= 4 and not t.isdigit()}


def _memo_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    import re

    words = {t for t in re.split(r"[^0-9A-Za-z一-鿿]+", text.upper()) if len(t) >= 4}
    # long CJK runs are common; treat 4+ char chunks as tokens
    return words


def _idf(rows: list[dict[str, Any]]) -> dict[str, int]:
    freq: dict[str, int] = {}
    for r in rows:
        for tok in _memo_tokens(r.get("memo")) | _ref_tokens(r.get("external_ref", "")):
            freq[tok] = freq.get(tok, 0) + 1
    return freq


def _pair_parts(
    bank: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    strategy: dict[str, Any],
    freq: dict[str, int],
    total_docs: int,
) -> tuple[dict[str, float], int]:
    """Score components for one bank group vs one ledger group."""
    weights = strategy["score_weights"]
    bank_sum = sum(_signed(r) for r in bank)
    ledger_sum = sum(_signed(r) for r in ledger)
    delta = ledger_sum - bank_sum
    tol = strategy["amount_tolerance_cents"]
    amount_part = 0.0
    if abs(delta) <= tol:
        if delta == 0:
            amount_part = weights["amount"]
        else:
            amount_part = weights["amount"] * (1 - abs(delta) / max(tol, 1)) * 0.85

    def _min_gap() -> int:
        return min(abs((_row_date(l) - _row_date(b)).days) for b in bank for l in ledger)

    gap = _min_gap()
    window = max(strategy["day_window"], 1)
    date_part = 0.0 if gap > strategy["day_window"] else weights["date"] * (1 - gap / (window + 1))

    b_refs = set().union(*[_ref_tokens(b.get("external_ref", "")) for b in bank])
    l_refs = set().union(*[_ref_tokens(l.get("external_ref", "")) for l in ledger])
    l_memo = set().union(*[_memo_tokens(l.get("memo")) for l in ledger])
    b_memo = set().union(*[_memo_tokens(b.get("memo")) for b in bank])
    shared_ref = b_refs & (l_refs | l_memo)
    shared_memo = (b_memo & (l_memo | l_refs)) - shared_ref

    def _weight(tok: str) -> float:
        df = freq.get(tok, 1)
        return 1.0 + math.log(max(total_docs, 1) / df)

    ref_hit = sum(_weight(t) for t in shared_ref)
    memo_hit = sum(_weight(t) for t in shared_memo)
    ref_part = min(weights["reference"], ref_hit * 7.0) + min(weights["counterparty"], memo_hit * 4.0)

    acct_part = 0.0
    bank_accounts = {b["account"] for b in bank}
    ledger_accounts = {l["account"] for l in ledger}
    if bank_accounts & ledger_accounts:
        acct_part = weights["account"]
    elif any(b["account"] in l["account"] or l["account"] in b["account"] for b in bank for l in ledger):
        acct_part = weights["account"] * 0.5

    parts = {
        "amount": amount_part,
        "date": date_part,
        "reference": ref_part,
        "account": acct_part,
    }
    return parts, delta


def _score_total(parts: dict[str, float], kind: str, strategy: dict[str, Any]) -> float:
    total = sum(parts.values())
    if kind == "split":
        total -= strategy["split_penalty"]
    elif kind == "merge":
        total -= strategy["merge_penalty"]
    return max(0.0, min(100.0, total))


def _cand_key(kind: str, bank_ids: Iterable[str], ledger_ids: Iterable[str]) -> str:
    b = "-".join(sorted(bank_ids))
    l = "-".join(sorted(ledger_ids))
    return f"{kind}:{b}|{l}"


def _reason_lines(
    bank: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    delta: int,
    gap: int,
    shared_ref: set[str],
    shared_memo: set[str],
    kind: str,
    strategy: dict[str, Any],
) -> list[str]:
    reasons = []
    if kind == "one_to_one":
        reasons.append(f"金额完全一致（{bank[0]['amount_cents'] / 100:.2f}）" if delta == 0 else f"金额差 {delta / 100:+.2f} 在容差内")
    elif kind == "split":
        reasons.append(
            f"1 笔银行 {sum(_signed(b) for b in bank) / 100:.2f} = {len(ledger)} 笔账务 "
            f"{'+'.join(f'{abs(_signed(l)) / 100:.2f}' for l in ledger)}（差 {delta / 100:+.2f}）"
        )
    else:
        reasons.append(
            f"{len(bank)} 笔银行 {'+'.join(f'{abs(_signed(b)) / 100:.2f}' for b in bank)} = 1 笔账务 "
            f"{abs(_signed(ledger[0])) / 100:.2f}（差 {delta / 100:+.2f}）"
        )
    reasons.append(f"日期相差 {gap} 天（窗口 ±{strategy['day_window']} 天）")
    if shared_ref:
        reasons.append("引用号/摘要匹配：" + "、".join(sorted(shared_ref)[:3]))
    elif shared_memo:
        reasons.append("摘要关键词：" + "、".join(sorted(shared_memo)[:3]))
    if bank[0]["account"] == ledger[0]["account"]:
        reasons.append(f"同一账号 {bank[0]['account']}")
    return reasons


def _account_ok(bank: list[dict[str, Any]], ledger: list[dict[str, Any]], strategy: dict[str, Any]) -> bool:
    """Cross-account comparison is allowed but only for amounts/refs when configured."""
    return True


def _base_filters(bank: list[dict[str, Any]], ledger: list[dict[str, Any]], strategy: dict[str, Any]) -> bool:
    if not bank or not ledger:
        return False
    bank_sum = sum(_signed(r) for r in bank)
    ledger_sum = sum(_signed(r) for r in ledger)
    if bank_sum == 0 or ledger_sum == 0:
        return False
    if (bank_sum > 0) != (ledger_sum > 0):
        return False
    if abs(ledger_sum - bank_sum) > strategy["amount_tolerance_cents"]:
        return False
    gap = min(abs((_row_date(l) - _row_date(b)).days) for b in bank for l in ledger)
    return gap <= strategy["day_window"]


def generate_candidates(
    bank_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> list[Candidate]:
    """Generate all explainable candidate groups for the strategy."""
    freq = _idf(bank_rows + ledger_rows)
    total_docs = max(len(bank_rows) + len(ledger_rows), 1)
    candidates: list[Candidate] = []

    def _add(kind: str, bank: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> None:
        if not _base_filters(bank, ledger, strategy):
            return
        parts, delta = _pair_parts(bank, ledger, strategy, freq, total_docs)
        score = _score_total(parts, kind, strategy)
        if score < strategy["min_score"]:
            return
        b_refs = set().union(*[_ref_tokens(b.get("external_ref", "")) for b in bank])
        l_refs = set().union(*[_ref_tokens(l.get("external_ref", "")) for l in ledger])
        l_memo = set().union(*[_memo_tokens(l.get("memo")) for l in ledger])
        b_memo = set().union(*[_memo_tokens(b.get("memo")) for b in bank])
        shared_ref = b_refs & (l_refs | l_memo)
        shared_memo = (b_memo & (l_memo | l_refs)) - shared_ref
        gap = min(abs((_row_date(l) - _row_date(b)).days) for b in bank for l in ledger)
        bank_ids = [b["id"] for b in bank]
        ledger_ids = [l["id"] for l in ledger]
        candidates.append(
            Candidate(
                key=_cand_key(kind, bank_ids, ledger_ids),
                kind=kind,
                bank_ids=bank_ids,
                ledger_ids=ledger_ids,
                amount_delta_cents=delta,
                score=score,
                reasons=_reason_lines(bank, ledger, delta, gap, shared_ref, shared_memo, kind, strategy),
                parts=parts,
            )
        )

    # 1:1
    for b in bank_rows:
        for l in ledger_rows:
            _add("one_to_one", [b], [l])

    # split: 1 bank -> 2..max_parts ledger rows
    if strategy["allow_split"]:
        max_parts = strategy["max_parts"]
        for b in bank_rows:
            sign = _signed(b)
            target_window = (sign > 0)
            pool = [
                l for l in ledger_rows
                if (_signed(l) > 0) == target_window
                and abs(_row_date(l) - _row_date(b)).days <= strategy["day_window"]
                and abs(_signed(l)) <= abs(sign) + strategy["amount_tolerance_cents"]
            ]
            pool.sort(key=lambda r: (_row_date(r), r["id"]))
            for n in range(2, max_parts + 1):
                if len(pool) < n:
                    break
                for combo in itertools.combinations(pool, n):
                    _add("split", [b], list(combo))

    # merge: 2..max_parts bank rows -> 1 ledger row
    if strategy["allow_merge"]:
        max_parts = strategy["max_parts"]
        for l in ledger_rows:
            sign = _signed(l)
            target_window = (sign > 0)
            pool = [
                b for b in bank_rows
                if (_signed(b) > 0) == target_window
                and abs(_row_date(b) - _row_date(l)).days <= strategy["day_window"]
                and abs(_signed(b)) <= abs(sign) + strategy["amount_tolerance_cents"]
            ]
            pool.sort(key=lambda r: (_row_date(r), r["id"]))
            for n in range(2, max_parts + 1):
                if len(pool) < n:
                    break
                for combo in itertools.combinations(pool, n):
                    _add("merge", list(combo), [l])

    # deterministic order: best score first, then key
    candidates.sort(key=lambda c: (-c.score, c.key))
    return candidates


# --------------------------------------------------------------------------- #
# plan solving
# --------------------------------------------------------------------------- #
def solve_plan(
    candidates: list[Candidate],
    *,
    locked: list[dict[str, Any]] | None = None,
    min_score: float | None = None,
    max_steps: int = 200000,
) -> dict[str, Any]:
    """Select a non-overlapping set of candidates.

    ``locked`` are effective manual matches that must be honoured (their rows
    are removed from the pool).  Returns ``{"selected": [...], "skipped": [...],
    "blocked_by_locked": [...]}``.
    """
    locked = locked or []
    picked: list[Candidate] = []
    used_bank: set[str] = set()
    used_ledger: set[str] = set()
    used_cand: set[str] = set()
    blocked: list[dict[str, Any]] = []

    for item in locked:
        for rid in item["bank_ids"]:
            used_bank.add(rid)
        for rid in item["ledger_ids"]:
            used_ledger.add(rid)
        # a manual match counts as "taken"; candidates that overlap it are skipped
        if item.get("key"):
            used_cand.add(item["key"])

    # candidate keys overlapping locked matches must not be selected
    def overlaps(c: Candidate) -> bool:
        return bool(set(c.bank_ids) & used_bank) or bool(set(c.ledger_ids) & used_ledger)

    for c in candidates:
        if c.key in used_cand:
            continue
        if overlaps(c):
            blocked.append({
                "key": c.key,
                "reason": "与已锁定的人工匹配冲突",
                "score": round(c.score, 2),
            })
            continue
        used_bank.update(c.bank_ids)
        used_ledger.update(c.ledger_ids)
        used_cand.add(c.key)
        picked.append(c)

    # Greedy above can be suboptimal after a locked set changes the pool.  Run a
    # small local improvement: try replacing one selected group with a better
    # unused overlapping group (only if strictly higher score and disjoint from
    # everything else).  Bounded by max_steps for determinism.
    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False
        for i, cur in enumerate(list(picked)):
            others_bank = set().union(*[set(p.bank_ids) for j, p in enumerate(picked) if j != i]) if len(picked) > 1 else set()
            others_ledger = set().union(*[set(p.ledger_ids) for j, p in enumerate(picked) if j != i]) if len(picked) > 1 else set()
            for cand in candidates:
                steps += 1
                if steps > max_steps:
                    break
                if cand.key in {p.key for p in picked}:
                    continue
                if not (set(cand.bank_ids) & set(cur.bank_ids) or set(cand.ledger_ids) & set(cur.ledger_ids)):
                    continue
                if set(cand.bank_ids) & others_bank or set(cand.ledger_ids) & others_ledger:
                    continue
                if cand.score > cur.score + 1e-9:
                    picked[i] = cand
                    improved = True
                    break
            if improved:
                break
        if steps > max_steps:
            break

    picked.sort(key=lambda c: (-c.score, c.key))
    return {
        "selected": [p.as_dict() for p in picked],
        "blocked_by_locked": blocked,
        "steps": steps,
    }


def explain_gap(
    bank_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attribute the raw balance difference to concrete causes (engine-level)."""
    bank_total = sum(_signed(r) for r in bank_rows)
    ledger_total = sum(_signed(r) for r in ledger_rows)
    raw_diff = ledger_total - bank_total

    paired_bank = {rid for m in matches for rid in m["bank_ids"]}
    paired_ledger = {rid for m in matches for rid in m["ledger_ids"]}
    match_delta = sum(m["amount_delta_cents"] for m in matches)

    unmatched_bank = [r for r in bank_rows if r["id"] not in paired_bank]
    unmatched_ledger = [r for r in ledger_rows if r["id"] not in paired_ledger]
    unmatched_bank_total = sum(_signed(r) for r in unmatched_bank)
    unmatched_ledger_total = sum(_signed(r) for r in unmatched_ledger)

    return {
        "bank_total_cents": bank_total,
        "ledger_total_cents": ledger_total,
        "raw_diff_cents": raw_diff,
        "matched_count": len(matches),
        "matched_pairs": sum(len(m["bank_ids"]) + len(m["ledger_ids"]) for m in matches),
        "match_delta_cents": match_delta,
        "unmatched_bank_count": len(unmatched_bank),
        "unmatched_ledger_count": len(unmatched_ledger),
        "unmatched_bank_cents": unmatched_bank_total,
        "unmatched_ledger_cents": unmatched_ledger_total,
        "unmatched_bank_ids": [r["id"] for r in unmatched_bank],
        "unmatched_ledger_ids": [r["id"] for r in unmatched_ledger],
        "residual_cents": raw_diff - match_delta - unmatched_ledger_total + unmatched_bank_total,
    }


def validate_plan(matches: list[dict[str, Any]]) -> list[str]:
    """Check the two hard invariants.  Returns human-readable violations."""
    problems = []
    seen: dict[str, str] = {}
    for m in matches:
        if not m.get("effective", True):
            continue
        for rid in m["bank_ids"] + m["ledger_ids"]:
            if rid in seen and seen[rid] != m["id"]:
                problems.append(f"流水 {rid} 同时被 {seen[rid]} 与 {m['id']} 占用")
            seen[rid] = m["id"]
    return problems


def validate_manual_group(
    bank: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Hard violations vs. warnings for a human-confirmed group.

    Hard violations mean the group cannot be a reconciliation relation under any
    strategy.  Warnings mean the group is legitimate but the human overrode an
    automatic rule (score, tolerance, day window, split/merge switch).
    """
    hard: list[str] = []
    warnings: list[str] = []
    if not bank or not ledger:
        return ["匹配两侧不能为空"], warnings
    if len(bank) > 1 and len(ledger) > 1:
        return ["不支持多对多匹配"], warnings
    if len(bank) > MANUAL_LIMITS["parts"] or len(ledger) > MANUAL_LIMITS["parts"]:
        hard.append(f"单侧最多 {MANUAL_LIMITS['parts']} 笔")
    bank_sum = sum(_signed(r) for r in bank)
    ledger_sum = sum(_signed(r) for r in ledger)
    if (bank_sum > 0) != (ledger_sum > 0):
        hard.append("借贷方向不一致")
    delta = ledger_sum - bank_sum
    if abs(delta) > MANUAL_LIMITS["amount_cents"]:
        hard.append(
            f"金额差 {delta / 100:+.2f} 元超过人工确认硬上限 "
            f"{MANUAL_LIMITS['amount_cents'] / 100:.2f} 元"
        )
    gap = min(abs((_row_date(l) - _row_date(b)).days) for b in bank for l in ledger)
    if gap > MANUAL_LIMITS["days"]:
        hard.append(f"日期相差 {gap} 天，超过人工确认硬上限 {MANUAL_LIMITS['days']} 天")
    if hard:
        return hard, warnings

    kind = classify_kind(len(bank), len(ledger))
    if kind == "split" and not strategy["allow_split"]:
        warnings.append("策略当前禁止拆单；确认将作为人工覆盖")
    if kind == "merge" and not strategy["allow_merge"]:
        warnings.append("策略当前禁止合单；确认将作为人工覆盖")
    if abs(delta) > strategy["amount_tolerance_cents"]:
        warnings.append(
            f"金额差 {delta / 100:+.2f} 元超过策略容差 "
            f"{strategy['amount_tolerance_cents'] / 100:.2f} 元"
        )
    if gap > strategy["day_window"]:
        warnings.append(f"日期相差 {gap} 天，超过策略窗口 {strategy['day_window']} 天")
    return hard, warnings


def classify_kind(n_bank: int, n_ledger: int) -> str | None:
    if n_bank == 1 and n_ledger == 1:
        return "one_to_one"
    if n_bank == 1 and n_ledger > 1:
        return "split"
    if n_bank > 1 and n_ledger == 1:
        return "merge"
    return None


def validate_group(
    bank: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> list[str]:
    """Structural validity of a group under a strategy (used for revalidation)."""
    problems: list[str] = []
    if not bank or not ledger:
        return ["匹配两侧不能为空"]
    kind = classify_kind(len(bank), len(ledger))
    if kind is None:
        return ["不支持多对多匹配"]
    if kind == "split" and not strategy["allow_split"]:
        problems.append("策略已禁止拆单")
    if kind == "merge" and not strategy["allow_merge"]:
        problems.append("策略已禁止合单")
    max_parts = strategy["max_parts"]
    if len(bank) > 1 and len(bank) > max_parts:
        problems.append(f"合单笔数 {len(bank)} 超过上限 {max_parts}")
    if len(ledger) > 1 and len(ledger) > max_parts:
        problems.append(f"拆单笔数 {len(ledger)} 超过上限 {max_parts}")
    bank_sum = sum(_signed(r) for r in bank)
    ledger_sum = sum(_signed(r) for r in ledger)
    if (bank_sum > 0) != (ledger_sum > 0):
        problems.append("借贷方向不一致")
    delta = ledger_sum - bank_sum
    if abs(delta) > strategy["amount_tolerance_cents"]:
        problems.append(
            f"金额差 {delta / 100:+.2f} 超过容差 {strategy['amount_tolerance_cents'] / 100:.2f}"
        )
    gap = min(abs((_row_date(l) - _row_date(b)).days) for b in bank for l in ledger)
    if gap > strategy["day_window"]:
        problems.append(f"日期相差 {gap} 天，超过窗口 {strategy['day_window']} 天")
    accounts = {b["account"] for b in bank} | {l["account"] for l in ledger}
    return problems


def score_group(
    bank: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> tuple[float, dict[str, float], int]:
    """Score an arbitrary group (manual confirmation evidence)."""
    kind = classify_kind(len(bank), len(ledger)) or "one_to_one"
    freq = _idf(all_rows)
    parts, delta = _pair_parts(bank, ledger, strategy, freq, max(len(all_rows), 1))
    return _score_total(parts, kind, strategy), parts, delta
