"""Offline console distinctions for pending, blocked and unknown sends."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import automatic_recovery, protocol_dispatch, streak_state, tasks
from webui import ops

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
WINDOW = {"enabled": False}


class PipelineConsoleTests(unittest.TestCase):
    def account(self):
        return {"username": "demo-pipeline", "unique_id": "demo-pipeline", "targets": ["Alice", "Bob", "Carol"]}

    def test_unattempted_blocked_target_does_not_inherit_account_attempt(self):
        account = self.account()
        item = ops._build_target_status(account, "Carol", NOW, WINDOW)
        failure = {"category": "browser_stage_timeout", "reason": "browser_stage_timeout stage=list_rows",
                   "lastAttemptAt": NOW.isoformat(), "attemptCount": 2, "affectedTargets": ["Bob", "Carol"]}
        blocked = ops._account_blocked_target_status(account, item, failure, NOW)
        self.assertEqual(blocked["status"], "account_blocked")
        self.assertEqual(blocked["lastAttemptAt"], "")
        self.assertEqual(blocked["attemptCount"], 0)
        self.assertEqual(blocked["accountLastAttemptAt"], NOW.isoformat())
        self.assertEqual(blocked["accountAttemptCount"], 2)
        self.assertIn("未执行", blocked["skipReason"])

    def test_barrier_without_history_is_unknown_not_pending(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW, detail="pre_enter_outcome_unknown_barrier")
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "unconfirmed")
        self.assertTrue(item["needsVerification"])
        self.assertEqual(item["confirmationSource"], "pre_enter_outcome_unknown_barrier")
        self.assertIn("不自动重发", item["skipReason"])
        self.assertIn("不代表已经发出", item["reason"])
        self.assertIn("不代表页面有回显", item["reason"])

    def test_strong_history_precedes_a_conservative_unknown_barrier(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW, detail="pre_enter_outcome_unknown_barrier")
        account["message_history"] = {"Bob": {
            "sentAt": NOW.isoformat(), "status": "confirmed", "confirmationLevel": "strong",
            "confirmationSource": "browser_server_receipt", "message": "synthetic greeting",
        }}
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "sent")
        self.assertFalse(item["needsVerification"])

    def test_previous_day_barrier_does_not_become_todays_unknown(self):
        account = self.account()
        streak_state.mark_sent_unverified(account, "Bob", strategy="browser", run_id="offline",
                                          now=NOW-timedelta(days=1), detail="pre_enter_outcome_unknown_barrier")
        item = ops._build_target_status(account, "Bob", NOW, WINDOW)
        self.assertEqual(item["status"], "unprocessed")

    def test_login_block_does_not_promise_automatic_recovery_tomorrow(self):
        account = self.account()
        item = ops._build_target_status(account, "Carol", NOW, WINDOW)
        failure = {"category": "login_required", "lastAttemptAt": NOW.isoformat(), "attemptCount": 1}
        blocked = ops._account_blocked_target_status(account, item, failure, NOW)
        self.assertNotIn("明天恢复", blocked["skipReason"])
        self.assertIn("重新登录", blocked["skipReason"])

    def test_7467_platform_tip_and_waiting_status_are_visible_across_days(self):
        account = self.account()
        streak_state.mark_failed(account, "Alice", category="send_rejected", reason="refused", retryable=False, now=NOW)
        tip = "给对方发送的消息已达上限，对方回复或互关后才能继续发送消息"
        receipt = {
            "call": "message_send", "ok": False, "protoRefusalUnderstood": True,
            "protoCheckCodes": {"status_code": 7467, "raw_check_code": 2},
            "rejection": {"key": "status_code", "value": 7467},
        }
        account["failure_queue"] = {"Alice": {
            "category": "send_rejected", "lastAttemptAt": NOW.isoformat(timespec="seconds"),
            "reason": "server refused; synthetic details", "serverReceipt": receipt,
        }}
        for when in (NOW, NOW + timedelta(days=1)):
            with self.subTest(when=when):
                item = ops._build_target_status(account, "Alice", when, WINDOW)
                self.assertEqual("waiting_for_reply", item["status"])
                self.assertEqual("待对方回复或互关", item["categoryLabel"])
                self.assertEqual(7467, item["rejectionCode"])
                self.assertEqual(tip, item["reason"])

        del account["failure_queue"]["Alice"]["serverReceipt"]
        account["failure_queue"]["Alice"]["reason"] = "status_code=7467"
        self.assertFalse(streak_state.waiting_for_reply_since(account, "Alice"))

    def test_waiting_target_is_skipped_by_bulk_schedules_and_send_claims_next_day(self):
        account = self.account()
        streak_state.mark_waiting_for_reply(account, "Alice", NOW)
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(["Bob", "Carol"], tasks._pending_unsent_targets(account, tomorrow)[0])
        self.assertEqual([], tasks._pending_failed_targets(account, tomorrow))
        self.assertEqual(["Bob", "Carol"], tasks._select_due_targets(account, WINDOW, tomorrow)[0])
        self.assertTrue(automatic_recovery._blocked_target(account, "Alice", tomorrow))
        for strategy in ("browser", "protocol"):
            with self.subTest(strategy=strategy):
                _, claimed = streak_state.claim_in_flight(
                    account, "Alice", run_id="test", strategy=strategy, now=tomorrow, allow_retry=True,
                )
                self.assertFalse(claimed)

    def test_protocol_rejection_records_a_hold_from_the_structured_check_message(self):
        account = self.account()
        failed = {
            "target": "Alice", "statusCode": 3, "success": False,
            "sendResultSummary": {"checkMsg": '{"status_code":7467,"raw_check_code":2}'},
            "sentAt": NOW.isoformat(),
        }
        self.assertTrue(protocol_dispatch._apply_protocol_runtime_state(
            [account], [account], {account["username"]: {"sent": [failed]}},
        ))
        self.assertEqual(NOW.isoformat(), streak_state.waiting_for_reply_since(account, "Alice"))
        _, token = streak_state.waiting_for_reply(account, "Alice")
        self.assertTrue(streak_state.release_waiting_for_reply(account, "Alice", token, NOW + timedelta(minutes=1)))
        self.assertFalse(streak_state.failure_entry(account, "Alice"))
        self.assertEqual("unprocessed", ops._build_target_status(account, "Alice", NOW + timedelta(minutes=1), WINDOW)["status"])

    def test_release_archives_old_receipt_and_restores_normal_plan(self):
        account = self.account()
        receipt = {"call": "message_send", "ok": False, "rejection": {"key": "status_code", "value": 7467}}
        account["failure_queue"] = {"Alice": {
            "category": "send_rejected", "lastAttemptAt": NOW.isoformat(timespec="seconds"), "serverReceipt": receipt,
        }}
        _, token = streak_state.waiting_for_reply(account, "Alice")
        released_at = NOW + timedelta(minutes=1)
        self.assertTrue(streak_state.release_waiting_for_reply(account, "Alice", token, released_at))
        self.assertEqual(receipt, account["failure_queue"]["Alice"]["serverReceipt"])
        self.assertFalse(streak_state.waiting_for_reply_since(account, "Alice"))
        self.assertFalse(streak_state.failure_entry(account, "Alice"))
        self.assertEqual("unprocessed", ops._build_target_status(account, "Alice", released_at, WINDOW)["status"])
        self.assertIn("Alice", tasks._pending_unsent_targets(account, released_at)[0])
        self.assertIn("Alice", tasks._select_due_targets(account, WINDOW, released_at)[0])

        self.assertTrue(streak_state.mark_waiting_for_reply(account, "Alice", released_at))
        self.assertFalse(streak_state.release_waiting_for_reply(account, "Alice", token, released_at))
        self.assertTrue(streak_state.waiting_for_reply_since(account, "Alice"))

    def test_release_keeps_a_distinct_failure_that_came_after_the_hold(self):
        account = self.account()
        streak_state.mark_waiting_for_reply(account, "Alice", NOW)
        failed_at = NOW + timedelta(minutes=1)
        streak_state.mark_failed(
            account, "Alice", category="friend_not_found", reason="unrelated", retryable=True, now=failed_at,
        )
        account["failure_queue"] = {"Alice": {
            "category": "friend_not_found", "lastAttemptAt": failed_at.isoformat(timespec="seconds"),
        }}
        _, token = streak_state.waiting_for_reply(account, "Alice")
        self.assertTrue(streak_state.release_waiting_for_reply(account, "Alice", token, failed_at + timedelta(seconds=1)))
        self.assertEqual("friend_not_found", streak_state.failure_entry(account, "Alice")["category"])
        self.assertEqual("friend_not_found", streak_state.target_state(account, "Alice", failed_at)["lastErrorCategory"])

    def test_target_identity_upgrade_preserves_release_over_stale_hold(self):
        account = self.account()
        streak_state.mark_failed(account, "Alice", category="send_rejected", reason="7467", retryable=False, now=NOW)
        streak_state.mark_waiting_for_reply(account, "Alice", NOW)
        stale = streak_state.target_state(account, "Alice", NOW)
        _, token = streak_state.waiting_for_reply(account, "Alice")
        released_at = NOW + timedelta(minutes=1)
        self.assertTrue(streak_state.release_waiting_for_reply(account, "Alice", token, released_at))
        old_ref = streak_state.resolve_target_ref(account, "Alice")
        account["target_states"]["peer:4242"] = dict(stale, targetRef="peer:4242")
        streak_state._upgrade_state_key(account, old_ref, "peer:4242", "Alice")
        self.assertFalse(streak_state.waiting_for_reply_since(account, "Alice"))
        self.assertEqual(streak_state.STATE_PENDING, streak_state.target_state(account, "Alice", released_at)["status"])


if __name__ == "__main__":
    unittest.main()
