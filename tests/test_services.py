"""跨服务集成测试：唯一占用、人工确认、策略变更、局部/全局重配、余额归因。"""
from __future__ import annotations

import json
import unittest

from harness import StackTestCase


class IngestTests(StackTestCase):
    def test_batch_insert_and_dedupe(self):
        stack = self.stack
        rows = stack.load_scenario()
        status, res = stack.call("ingest", "/api/batches", "POST",
                                 {"source": "bank", "label": "重复", "rows": rows["bank"]})
        self.assertEqual(status, 201)
        self.assertEqual(res["inserted_count"], 0)
        self.assertEqual(len(res["duplicates"]), len(rows["bank"]))
        self.assertTrue(all(d["reason"] == "已存在" for d in res["duplicates"]))

    def test_validation_errors(self):
        stack = self.stack
        bad = [
            {"source": "nope", "rows": [{"external_ref": "x"}]},
            {"source": "bank", "rows": []},
            {"source": "bank", "rows": [{"external_ref": "r1", "account": "a", "value_date": "2026-13-01",
                                         "amount_cents": 100, "direction": "credit"}]},
            {"source": "bank", "rows": [{"external_ref": "r1", "account": "a", "value_date": "2026-09-01",
                                         "amount_cents": 0, "direction": "credit"}]},
            {"source": "bank", "rows": [{"external_ref": "r1", "account": "a", "value_date": "2026-09-01",
                                         "amount_cents": "100", "direction": "credit"}]},
        ]
        for body in bad:
            status, data = stack.call("ingest", "/api/batches", "POST", body)
            self.assertEqual(status, 422, body)
            self.assertEqual(data["error"]["code"], "validation_error")

    def test_late_rows_flagged_and_not_counted_as_duplicate(self):
        stack = self.stack
        stack.load_scenario("base")
        with open("web/static/late.json", encoding="utf-8") as fh:
            late_rows = json.load(fh)["bank"]
        status, res = stack.call("ingest", "/api/batches", "POST",
                                 {"source": "bank", "label": "迟到", "rows": late_rows})
        self.assertEqual(status, 201)
        self.assertEqual(res["inserted_count"], 1)
        self.assertEqual(res["late_count"], 1)
        status, rows = stack.call("ingest", "/api/rows?late=1")
        self.assertEqual(rows["total"], 1)
        self.assertTrue(rows["rows"][0]["late"])

    def test_health_reports_uptime(self):
        data = self.stack.ok("ingest", "/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "ingest")


class MatchingTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.stack.load_scenario()
        self.ids = self.stack.row_ids()

    def run_global(self):
        return self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})

    def test_scenario_matches_all_expected_kinds(self):
        res = self.run_global()
        kinds = sorted(m["kind"] for m in res["matches"])
        self.assertEqual(kinds, ["merge", "one_to_one", "one_to_one", "one_to_one", "one_to_one", "split"])
        flat = {tuple(m["bank_ids"] + m["ledger_ids"]) for m in res["matches"]}
        self.assertIn((self.ids["BK-2026-0001"], self.ids["GL-2026-0001"]), flat)
        self.assertIn((self.ids["BK-2026-0003"], self.ids["GL-2026-0003"], self.ids["GL-2026-0004"]), flat)

    def test_invariant_no_row_double_booked(self):
        res = self.run_global()
        seen = {}
        for m in res["matches"]:
            for rid in m["bank_ids"] + m["ledger_ids"]:
                self.assertNotIn(rid, seen, "row used by two matches")
                seen[rid] = m["id"]
        # unmatched rows must not appear anywhere
        self.assertNotIn(self.ids["BK-2026-0009"], seen)
        self.assertNotIn(self.ids["GL-2026-0008"], seen)

    def test_rerun_is_idempotent(self):
        first = self.run_global()
        second = self.run_global()
        self.assertEqual(len(first["matches"]), len(second["matches"]))
        self.assertEqual(
            sorted(tuple(m["bank_ids"] + m["ledger_ids"]) for m in first["matches"]),
            sorted(tuple(m["bank_ids"] + m["ledger_ids"]) for m in second["matches"]),
        )

    def test_candidates_explain_evidence(self):
        self.run_global()
        data = self.stack.ok("matcher", f"/api/candidates?row_id={self.ids['BK-2026-0003']}")
        self.assertTrue(data["candidates"])
        split = [c for c in data["candidates"] if c["kind"] == "split"][0]
        self.assertTrue(any("拆" in r or "=" in r for r in split["reasons"]))
        self.assertGreater(split["parts"]["amount"], 0)
        self.assertLessEqual(split["score"], 100)

    def test_reject_suppresses_group_on_rerun(self):
        res = self.run_global()
        target = [m for m in res["matches"] if m["bank_ids"] == [self.ids["BK-2026-0001"]]][0]
        self.stack.ok("matcher", f"/api/matches/{target['id']}/reject", "POST", {"note": "合同号不符"})
        rerun = self.run_global()
        self.assertNotIn(self.ids["BK-2026-0001"], {i for m in rerun["matches"] for i in m["bank_ids"]})
        self.assertGreaterEqual(rerun["summary"]["suppressed_rejected"], 1)
        # the rejection itself is retained for audit
        kept = self.stack.ok("matcher", f"/api/matches/{target['id']}")
        self.assertEqual(kept["status"], "rejected")
        self.assertEqual(kept["note"], "合同号不符")

    def test_reject_requires_reason(self):
        res = self.run_global()
        status, data = self.stack.call("matcher", f"/api/matches/{res['matches'][0]['id']}/reject", "POST", {})
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "validation_error")

    def test_confirm_blocks_other_occupancy(self):
        res = self.run_global()
        b = self.ids["BK-2026-0001"]
        l1 = self.ids["GL-2026-0001"]
        l2 = self.ids["GL-2026-0002"]
        auto = [m for m in res["matches"] if m["bank_ids"] == [b]][0]
        first = self.stack.ok("matcher", f"/api/matches/{auto['id']}/confirm", "POST", {"note": "人工复核"})
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["origin"], "manual")
        status, data = self.stack.call("matcher", "/api/manual", "POST", {"bank_ids": [b], "ledger_ids": [l2]})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "row_already_matched")
        hit = [d for d in data["error"]["details"] if b in d["overlap_row_ids"]]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["status"], "confirmed")
        self.assertEqual(hit[0]["match_id"], first["id"])

    def test_manual_warnings_need_acknowledgement(self):
        pair = {"bank_ids": [self.ids["BK-2026-0010"]], "ledger_ids": [self.ids["GL-2026-0009"]]}
        status, data = self.stack.call("matcher", "/api/manual", "POST", {**pair, "confirm": True})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "warnings_unacknowledged")
        self.assertTrue(data["error"]["details"])
        ok = self.stack.ok("matcher", "/api/manual", "POST", {**pair, "confirm": True, "acknowledge_warnings": True})
        self.assertEqual(ok["status"], "confirmed")
        self.assertTrue(any("容差" in w for w in ok["warnings"]))

    def test_manual_hard_limit_cannot_be_overridden(self):
        hb = self.stack.ok("ingest", "/api/batches", "POST", {
            "source": "bank",
            "rows": [{"external_ref": "HB-1", "account": "6222-8801", "value_date": "2026-09-20",
                      "amount_cents": 100000, "direction": "credit"}]})
        hl = self.stack.ok("ingest", "/api/batches", "POST", {
            "source": "ledger",
            "rows": [{"external_ref": "HL-1", "account": "6222-8801", "value_date": "2026-09-20",
                      "amount_cents": 50000, "direction": "credit"}]})
        b, l = hb["inserted"][0]["id"], hl["inserted"][0]["id"]
        status, data = self.stack.call("matcher", "/api/manual", "POST",
                                       {"bank_ids": [b], "ledger_ids": [l], "acknowledge_warnings": True})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "evidence_insufficient")

    def test_opposite_direction_group_rejected(self):
        hb = self.stack.ok("ingest", "/api/batches", "POST", {
            "source": "bank",
            "rows": [{"external_ref": "DB-1", "account": "6222-8801", "value_date": "2026-09-20",
                      "amount_cents": 100000, "direction": "credit"}]})
        hl = self.stack.ok("ingest", "/api/batches", "POST", {
            "source": "ledger",
            "rows": [{"external_ref": "DL-1", "account": "6222-8801", "value_date": "2026-09-20",
                      "amount_cents": 100000, "direction": "debit"}]})
        status, data = self.stack.call("matcher", "/api/manual", "POST", {
            "bank_ids": [hb["inserted"][0]["id"]], "ledger_ids": [hl["inserted"][0]["id"]],
            "acknowledge_warnings": True})
        self.assertEqual(status, 409)
        self.assertIn("方向", " ".join(data["error"]["details"]))


class StrategyChangeTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.stack.load_scenario()
        self.ids = self.stack.row_ids()
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.manual = self.stack.ok("matcher", "/api/manual", "POST", {
            "bank_ids": [self.ids["BK-2026-0010"]], "ledger_ids": [self.ids["GL-2026-0009"]],
            "confirm": True, "acknowledge_warnings": True})

    def test_strategy_hash_changes(self):
        before = self.stack.ok("matcher", "/api/strategy")["hash"]
        self.stack.ok("matcher", "/api/strategy", "PUT", {"day_window": 5})
        after = self.stack.ok("matcher", "/api/strategy")["hash"]
        self.assertNotEqual(before, after)

    def test_invalid_strategy_rejected(self):
        status, data = self.stack.call("matcher", "/api/strategy", "PUT", {"day_window": 99})
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "invalid_strategy")

    def test_manual_stays_effective_when_strategy_unchanged(self):
        res = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.assertEqual(res["summary"]["stale_manual"], 0)
        m = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}")
        self.assertEqual(m["status"], "confirmed")
        self.assertTrue(m["effective"])

    def test_manual_becomes_stale_when_strategy_invalidates_it(self):
        self.stack.ok("matcher", "/api/strategy", "PUT", {"amount_tolerance_cents": 0, "day_window": 1})
        preview = self.stack.ok("matcher", "/api/run", "POST", {"mode": "preview"})
        self.assertEqual([s["id"] for s in preview["would_stale"]], [self.manual["id"]])
        res = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.assertEqual(res["summary"]["stale_manual"], 1)
        m = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}")
        self.assertEqual(m["status"], "stale")
        self.assertFalse(m["effective"])
        # not silently re-pointed: same rows, still manual, still recorded
        self.assertEqual(m["bank_ids"], [self.ids["BK-2026-0010"]])
        self.assertEqual(m["ledger_ids"], [self.ids["GL-2026-0009"]])
        self.assertEqual(m["origin"], "manual")
        # and no automatic match may take those rows
        eff = self.stack.ok("matcher", "/api/effective")["matches"]
        touched = {i for x in eff for i in x["bank_ids"] + x["ledger_ids"]}
        self.assertNotIn(self.ids["BK-2026-0010"], touched)

    def test_stale_requires_explicit_reconfirm(self):
        self.stack.ok("matcher", "/api/strategy", "PUT", {"amount_tolerance_cents": 0, "day_window": 1})
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        status, data = self.stack.call("matcher", f"/api/matches/{self.manual['id']}/confirm", "POST", {})
        self.assertEqual(status, 409)
        again = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}/confirm", "POST",
                              {"acknowledge_warnings": True, "note": "复核后确认"})
        self.assertEqual(again["status"], "confirmed")
        self.assertTrue(again["effective"])

    def test_stale_is_not_auto_resurrected(self):
        self.stack.ok("matcher", "/api/strategy", "PUT", {"amount_tolerance_cents": 0, "day_window": 1})
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.stack.ok("matcher", "/api/strategy", "PUT", {"amount_tolerance_cents": 1000, "day_window": 3})
        res = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        # stale confirmations are never auto-resurrected
        self.assertEqual(res["summary"]["locked_manual"], 0)
        self.assertEqual(res["summary"]["reserved_stale"], 1)
        m = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}")
        self.assertEqual(m["status"], "stale")
        # and their rows are not handed to a different automatic relation
        eff = self.stack.ok("matcher", "/api/effective")["matches"]
        touched = {i for x in eff for i in x["bank_ids"] + x["ledger_ids"]}
        self.assertNotIn(self.ids["BK-2026-0010"], touched)
        self.assertNotIn(self.ids["GL-2026-0009"], touched)

    def test_release_must_be_explicit(self):
        status, data = self.stack.call("matcher", "/api/run", "POST",
                                       {"mode": "local", "release_match_ids": [self.manual["id"]]})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "manual_locked")
        released = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}/release", "POST",
                                 {"mode": "none", "note": "改由自动规则处理"})
        self.assertEqual(released["released"]["status"], "released")
        self.assertFalse(released["released"]["effective"])
        self.assertTrue(released["released"]["note"])
        # the audit record survives a re-plan
        kept = self.stack.ok("matcher", f"/api/matches/{self.manual['id']}")
        self.assertEqual(kept["status"], "released")
        # released rows go back to the pool, but nothing is re-confirmed silently
        run = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        again = [m for m in run["matches"]
                 if self.ids["BK-2026-0010"] in m["bank_ids"] + m["ledger_ids"]]
        self.assertTrue(all(m["origin"] == "auto" and m["status"] == "proposed" for m in again))
        confirmed = self.stack.ok("matcher", "/api/matches?status=confirmed")["matches"]
        self.assertEqual(confirmed, [])


class ReplanCostTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.stack.load_scenario()
        self.ids = self.stack.row_ids()
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})

    def test_local_replan_touches_only_released_rows(self):
        before = {m["id"]: m for m in self.stack.ok("matcher", "/api/effective")["matches"]}
        target = [m for m in before.values() if m["bank_ids"] == [self.ids["BK-2026-0001"]]][0]
        run = self.stack.ok("matcher", "/api/run", "POST",
                            {"mode": "local", "release_match_ids": [target["id"]]})
        after = {m["id"]: m for m in self.stack.ok("matcher", "/api/effective")["matches"]}
        untouched_before = {k for k in before if k != target["id"]}
        self.assertTrue(untouched_before.issubset(after), "无关匹配不得被改动")
        self.assertEqual(run["summary"]["locked_auto"], len(untouched_before))

    def test_local_is_cheaper_than_global_in_candidates(self):
        target = [m for m in self.stack.ok("matcher", "/api/effective")["matches"]
                  if m["bank_ids"] == [self.ids["BK-2026-0003"]]][0]
        local = self.stack.ok("matcher", "/api/run", "POST",
                              {"mode": "local", "release_match_ids": [target["id"]]})
        glob = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.assertLess(local["summary"]["candidates_generated"], glob["summary"]["candidates_generated"])

    def test_local_requires_explicit_release_ids(self):
        status, data = self.stack.call("matcher", "/api/run", "POST", {"mode": "local"})
        self.assertEqual(status, 422)

    def test_local_cannot_widen_scope_by_omission(self):
        target = [m for m in self.stack.ok("matcher", "/api/effective")["matches"]
                  if m["kind"] == "split"][0]
        local = self.stack.ok("matcher", "/api/run", "POST",
                              {"mode": "local", "release_match_ids": [target["id"]]})
        # the released split may be re-formed; nothing else changes
        eff = self.stack.ok("matcher", "/api/effective")["matches"]
        self.assertEqual(len(eff), 6)
        self.assertGreaterEqual(local["summary"]["locked_auto"], 5)


class BalanceTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.stack.load_scenario()
        self.ids = self.stack.row_ids()
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})

    def test_identity_holds(self):
        data = self.stack.ok("balance", "/api/overview")
        self.assertEqual(data["identity_check_cents"], 0)
        self.assertEqual(
            data["raw_diff_cents"],
            data["matched_delta_cents"] + data["unmatched_ledger"]["total_cents"]
            - data["unmatched_bank"]["total_cents"],
        )

    def test_period_contributions_sum_to_diff(self):
        data = self.stack.ok("balance", "/api/overview")
        self.assertEqual(data["period_contribution_total_cents"], data["raw_diff_cents"])
        self.assertEqual(data["period_check_cents"], 0)

    def test_unmatched_are_explained(self):
        data = self.stack.ok("balance", "/api/overview")
        bank_refs = {r["external_ref"] for r in data["unmatched_bank"]["rows"]}
        ledger_refs = {r["external_ref"] for r in data["unmatched_ledger"]["rows"]}
        self.assertIn("BK-2026-0009", bank_refs)
        self.assertIn("GL-2026-0008", ledger_refs)
        amounts = [r for r in data["unmatched_bank"]["rows"] if r["external_ref"] == "BK-2026-0010"]
        self.assertEqual(amounts[0]["bucket"], "amount")
        self.assertIn("hint", amounts[0])

    def test_late_row_shows_up_in_late_bucket(self):
        with open("web/static/late.json", encoding="utf-8") as fh:
            late = json.load(fh)
        for source in ("bank", "ledger"):
            self.stack.ok("ingest", "/api/batches", "POST", {"source": source, "rows": late[source]})
        data = self.stack.ok("balance", "/api/overview")
        late_refs = {r["external_ref"] for r in data["unmatched_bank"]["rows"] if r["bucket"] == "late"}
        late_refs |= {r["external_ref"] for r in data["unmatched_ledger"]["rows"] if r["bucket"] == "late"}
        self.assertIn("BK-2026-0008", late_refs)
        self.assertIn("GL-2026-0007", late_refs)
        # before re-matching they are unmatched and fully explained
        self.assertEqual(data["identity_check_cents"], 0)

    def test_degraded_when_matcher_down(self):
        self.stack.matcher.close()
        data = self.stack.ok("balance", "/api/overview")
        self.assertTrue(data["matcher_down"])
        self.assertEqual(data["totals"]["effective_matches"], 0)
        self.assertEqual(data["matched_delta_cents"], 0)
        status, snap = self.stack.call("balance", "/api/snapshot", "POST", {})
        self.assertEqual(status, 409)
        self.assertEqual(snap["error"]["code"], "matcher_down")

    def test_snapshot_and_drift(self):
        first = self.stack.ok("balance", "/api/snapshot", "POST", {})
        self.assertEqual(first["identity_check_cents"], 0)
        drift = self.stack.ok("balance", "/api/drift")
        self.assertEqual(drift["changes"]["diff_cents"], 0)
        self.assertFalse(drift["strategy_changed"])


class RecoveryTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.stack.load_scenario()
        self.ids = self.stack.row_ids()

    def test_interrupted_run_rolls_back(self):
        good = self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        before = sorted(m["id"] for m in self.stack.ok("matcher", "/api/effective")["matches"])
        status, data = self.stack.call("matcher", "/api/simulate/interrupt", "POST", {"fail_after": 0})
        self.assertEqual(status, 500)
        self.assertEqual(data["error"]["code"], "simulated_interrupt")
        after = sorted(m["id"] for m in self.stack.ok("matcher", "/api/effective")["matches"])
        self.assertEqual(before, after)
        self.assertEqual(len(after), len(good["matches"]))
        events = self.stack.ok("matcher", "/api/simulate/events")["events"]
        self.assertEqual(events[0]["outcome"], "rolled_back")

    def test_outage_returns_503_then_recovers(self):
        self.stack.ok("matcher", "/api/simulate/outage", "POST", {"seconds": 1})
        status, data = self.stack.call("matcher", "/api/matches")
        self.assertEqual(status, 503)
        self.assertEqual(data["error"]["code"], "matcher_outage")
        import time
        time.sleep(1.1)
        self.stack.ok("matcher", "/api/matches")
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        self.assertEqual(self.stack.ok("balance", "/api/overview")["identity_check_cents"], 0)

    def test_plan_preview_does_not_write(self):
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "global"})
        before = self.stack.ok("matcher", "/api/effective")["matches"]
        self.stack.ok("matcher", "/api/run", "POST", {"mode": "preview"})
        after = self.stack.ok("matcher", "/api/effective")["matches"]
        self.assertEqual([m["id"] for m in before], [m["id"] for m in after])


if __name__ == "__main__":
    unittest.main()
