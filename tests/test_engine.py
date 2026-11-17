"""匹配引擎单元测试（无需启动服务）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recon.matching import (  # noqa: E402
    StrategyError,
    classify_kind,
    explain_gap,
    generate_candidates,
    normalize_strategy,
    solve_plan,
    strategy_hash,
    validate_manual_group,
    validate_plan,
)


def mkrow(rid, source, date, yuan, direction="credit", account="6222-8801", ref=None, memo="", late=False):
    cents = round(abs(yuan) * 100)
    return {
        "id": rid,
        "source": source,
        "external_ref": ref or rid,
        "account": account,
        "value_date": date,
        "posted_at": f"{date}T10:00:00Z",
        "amount_cents": cents,
        "direction": direction,
        "counterparty": "",
        "memo": memo,
        "currency": "CNY",
        "late": late,
    }


class StrategyTests(unittest.TestCase):
    def test_defaults_are_complete(self):
        s = normalize_strategy(None)
        for key in ("day_window", "amount_tolerance_cents", "min_score", "score_weights"):
            self.assertIn(key, s)

    def test_hash_is_stable_and_sensitive(self):
        a = normalize_strategy({"day_window": 3})
        b = normalize_strategy({"day_window": 3})
        c = normalize_strategy({"day_window": 4})
        self.assertEqual(strategy_hash(a), strategy_hash(b))
        self.assertNotEqual(strategy_hash(a), strategy_hash(c))

    def test_rejects_bad_values(self):
        with self.assertRaises(StrategyError):
            normalize_strategy({"day_window": -1})
        with self.assertRaises(StrategyError):
            normalize_strategy({"min_score": 101})
        with self.assertRaises(StrategyError):
            normalize_strategy({"score_weights": {"amount": "high"}})
        with self.assertRaises(StrategyError):
            normalize_strategy({"score_weights": {"nope": 3}})
        with self.assertRaises(StrategyError):
            normalize_strategy({"allow_split": "yes"})
        with self.assertRaises(StrategyError):
            normalize_strategy({"max_parts": 1})


class CandidateTests(unittest.TestCase):
    def test_one_to_one(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0)]
        cands = generate_candidates(bank, ledger, normalize_strategy(None))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].kind, "one_to_one")
        self.assertEqual(cands[0].amount_delta_cents, 0)
        self.assertGreaterEqual(cands[0].score, 80)
        self.assertTrue(any("金额" in r for r in cands[0].reasons))

    def test_split_and_merge(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 4800.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 3000.0), mkrow("l2", "ledger", "2026-09-02", 1800.0)]
        cands = generate_candidates(bank, ledger, normalize_strategy(None))
        kinds = {c.kind for c in cands}
        self.assertIn("split", kinds)
        split = [c for c in cands if c.kind == "split"][0]
        self.assertEqual(split.amount_delta_cents, 0)

        bank2 = [mkrow("b1", "bank", "2026-09-01", 2000.0), mkrow("b2", "bank", "2026-09-01", 1500.0),
                 mkrow("b3", "bank", "2026-09-02", 500.0)]
        ledger2 = [mkrow("l1", "ledger", "2026-09-02", 4000.0)]
        cands2 = generate_candidates(bank2, ledger2, normalize_strategy(None))
        merge = [c for c in cands2 if c.kind == "merge"]
        self.assertTrue(merge)
        self.assertEqual(merge[0].amount_delta_cents, 0)

    def test_opposite_directions_never_match(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0, direction="credit")]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0, direction="debit")]
        self.assertEqual(generate_candidates(bank, ledger, normalize_strategy(None)), [])

    def test_day_window_respected(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-20", 100.0)]
        cands = generate_candidates(bank, ledger, normalize_strategy({"day_window": 3}))
        self.assertEqual(cands, [])

    def test_disabling_split_removes_split(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 4800.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 3000.0), mkrow("l2", "ledger", "2026-09-02", 1800.0)]
        cands = generate_candidates(bank, ledger, normalize_strategy({"allow_split": False}))
        self.assertNotIn("split", {c.kind for c in cands})

    def test_amount_tolerance_bounds(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0)]
        fine = [mkrow("l1", "ledger", "2026-09-01", 100.5)]
        coarse = [mkrow("l2", "ledger", "2026-09-01", 102.0)]
        # at the tolerance edge the amount component is ~0, so lower the score floor
        strict = normalize_strategy({"amount_tolerance_cents": 50, "min_score": 0})
        self.assertTrue(generate_candidates(bank, fine, strict))
        self.assertFalse(generate_candidates(bank, coarse, strict))

    def test_deterministic_order(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0), mkrow("b2", "bank", "2026-09-02", 200.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0), mkrow("l2", "ledger", "2026-09-02", 200.0)]
        s = normalize_strategy(None)
        first = [c.key for c in generate_candidates(bank, ledger, s)]
        second = [c.key for c in generate_candidates(list(reversed(bank)), list(reversed(ledger)), s)]
        self.assertEqual(first, second)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.strategy = normalize_strategy(None)

    def test_no_row_used_twice(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0), mkrow("b2", "bank", "2026-09-02", 100.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0)]
        cands = generate_candidates(bank, ledger, self.strategy)
        plan = solve_plan(cands)
        self.assertEqual(len(plan["selected"]), 1)
        used = plan["selected"][0]["bank_ids"]
        self.assertEqual(len(used), 1)
        self.assertEqual(validate_plan(
            [{"id": s["key"], "bank_ids": s["bank_ids"], "ledger_ids": s["ledger_ids"], "effective": True}
             for s in plan["selected"]]
        ), [])

    def test_locked_manual_blocks_overlapping_auto(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0)]
        cands = generate_candidates(bank, ledger, self.strategy)
        locked = [{"id": "m1", "bank_ids": ["b1"], "ledger_ids": ["l1"], "effective": True, "key": None}]
        plan = solve_plan(cands, locked=locked)
        self.assertEqual(plan["selected"], [])
        self.assertEqual(len(plan["blocked_by_locked"]), 1)

    def test_higher_scoring_group_wins(self):
        # b1 could pair with l1 (exact) or be part of a split with l2+l3 (worse)
        bank = [mkrow("b1", "bank", "2026-09-01", 300.0)]
        ledger = [
            mkrow("l1", "ledger", "2026-09-01", 300.0, memo="合同 HT-9"),
            mkrow("l2", "ledger", "2026-09-01", 100.0),
            mkrow("l3", "ledger", "2026-09-02", 200.0),
        ]
        cands = generate_candidates(bank, ledger, self.strategy)
        plan = solve_plan(cands)
        self.assertEqual(len(plan["selected"]), 1)
        self.assertEqual(plan["selected"][0]["kind"], "one_to_one")


class ManualLimitTests(unittest.TestCase):
    def setUp(self):
        self.strategy = normalize_strategy(None)

    def test_within_hard_limits_but_outside_strategy_is_warning_only(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 5000.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 4998.0)]
        hard, warnings = validate_manual_group(bank, ledger, self.strategy)
        self.assertEqual(hard, [])
        self.assertTrue(any("容差" in w for w in warnings))

    def test_beyond_hard_limit_is_rejected(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 5000.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 4000.0)]
        hard, _ = validate_manual_group(bank, ledger, self.strategy)
        self.assertTrue(hard)

    def test_many_to_many_kind(self):
        self.assertIsNone(classify_kind(2, 2))
        self.assertEqual(classify_kind(2, 1), "merge")
        self.assertEqual(classify_kind(1, 3), "split")


class ExplainTests(unittest.TestCase):
    def test_gap_is_fully_attributed(self):
        bank = [mkrow("b1", "bank", "2026-09-01", 100.0), mkrow("b2", "bank", "2026-09-02", 50.0)]
        ledger = [mkrow("l1", "ledger", "2026-09-01", 100.0), mkrow("l2", "ledger", "2026-09-03", 70.0)]
        matches = [{"id": "m1", "bank_ids": ["b1"], "ledger_ids": ["l1"], "amount_delta_cents": 0}]
        gap = explain_gap(bank, ledger, matches)
        self.assertEqual(gap["raw_diff_cents"], 20 * 100)
        self.assertEqual(gap["residual_cents"], 0)
        self.assertEqual(gap["unmatched_bank_ids"], ["b2"])


if __name__ == "__main__":
    unittest.main()


class AttributionIdentityTests(unittest.TestCase):
    """The balance identity must hold for arbitrary generated data."""

    def test_identity_for_generated_pairs(self):
        import random

        rng = random.Random(20261117)
        for trial in range(25):
            bank, ledger = [], []
            for i in range(rng.randint(3, 8)):
                amt = round(rng.uniform(-500, 500), 2)
                direction = "credit" if amt > 0 else "debit"
                bank.append(mkrow(f"b{i}", "bank", "2026-09-0%d" % rng.randint(1, 9), abs(amt), direction))
            for i in range(rng.randint(3, 8)):
                amt = round(rng.uniform(-500, 500), 2)
                direction = "credit" if amt > 0 else "debit"
                ledger.append(mkrow(f"l{i}", "ledger", "2026-09-0%d" % rng.randint(1, 9), abs(amt), direction))
            strategy = normalize_strategy({"min_score": 0})
            cands = generate_candidates(bank, ledger, strategy)
            plan = solve_plan(cands)
            gap = explain_gap(bank, ledger, plan["selected"])
            self.assertEqual(gap["residual_cents"], 0, f"trial {trial}")
            used = [i for m in plan["selected"] for i in m["bank_ids"] + m["ledger_ids"]]
            self.assertEqual(len(used), len(set(used)), f"trial {trial}: duplicate row")
