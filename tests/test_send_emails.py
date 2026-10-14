"""Production safety of send_emails.py - every failure case found in the audit.

Each test works in a temporary copy of the project with a FAKE Brevo (tests/support.py) that
keeps its own record of every email it accepted - the ground truth for duplicates and the
daily limit. Production sending is switched on ONLY inside these tests, in memory.
"""

import io
import os
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

import support
from support import (MASTER_TEMPLATE, ORIGINAL_TEMPLATE, Clock, FakeBrevo, Killed, auto_answer, good_data, ist,
                     make_project, sample_data, write_batches, write_data_file)

import send_emails
import send_state
import send_test


class ProductionTestCase(unittest.TestCase):
    SUBSCRIBERS = 700
    DAILY_LIMIT = 300

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.clock = Clock(ist(2026, 10, 14, 10, 0))
        self.emails = make_project(self.dir, self.SUBSCRIBERS, self.clock().date())
        self.brevo = FakeBrevo(self.clock, MASTER_TEMPLATE.read_text(encoding="utf-8"))
        self.patches = [
            mock.patch.object(send_emails, "PROJECT_DIR", self.dir),
            mock.patch.object(send_emails, "PRODUCTION_SENDING_ENABLED", True),  # this test only
            mock.patch.object(send_emails, "DAILY_LIMIT", self.DAILY_LIMIT),
            mock.patch.object(send_emails, "make_client", lambda key: self.brevo),
            mock.patch.object(send_emails, "clock", self.clock),
            mock.patch.object(send_state, "now", self.clock),
            mock.patch.object(send_emails, "sleep", lambda seconds: None),
            mock.patch.object(send_emails, "ask", auto_answer),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self._tmp.cleanup()

    # ---- helpers -----------------------------------------------------------------------
    def at(self, *when):
        """Move the clock and confirm the content for that day (READY_TO_SEND_ON)."""
        self.clock.now = ist(*when)
        write_data_file(self.dir / "newsletter" / "newsletter_data.txt", good_data(self.clock().date()))

    def run_cmd(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = send_emails.main(list(args))
        return code, output.getvalue()

    def start_round(self):
        code, out = self.run_cmd("--new-round")
        self.assertEqual(code, 0, out)
        self.assertIn("started", out)

    def send(self):
        return self.run_cmd("--send")

    def log(self):
        return send_state.SendLog(self.dir / "logs" / "production_send_log.csv")

    def assert_no_duplicates_per_round(self):
        per_round = Counter((e, next(t for t in tags if t.startswith("coinedition-round-")))
                            for _, e, tags in self.brevo.accepted)
        repeated = [key for key, n in per_round.items() if n > 1]
        self.assertEqual(repeated, [], "someone got the same round's email twice")

    def assert_never_more_than_limit_in_24_hours(self, limit=None):
        limit = limit or self.DAILY_LIMIT
        times = sorted(t for t, _, _ in self.brevo.accepted)
        for i, t in enumerate(times):
            window = sum(1 for u in times[:i + 1] if u > t - timedelta(hours=24))
            self.assertLessEqual(window, limit, f"{window} emails in the 24 hours up to {t}")


class DailyLimitAndRoundTests(ProductionTestCase):
    def test_switch_off_blocks_everything(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        with mock.patch.object(send_emails, "PRODUCTION_SENDING_ENABLED", False):
            code, out = self.send()
        self.assertEqual(code, 1)
        self.assertIn("switched OFF", out)
        self.assertEqual(self.brevo.calls, 0)

    def test_300_a_day_over_several_days_then_complete(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.assertEqual(self.send()[0], 0)
        self.assertEqual(len(self.brevo.accepted), 300)
        self.at(2026, 10, 14, 15, 0)                       # same day again
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (1, 300))
        self.assertIn("No emails may be sent right now", out)
        self.assertIn("More can be sent after 2026-10-15 10:00", out)
        self.at(2026, 10, 15, 9, 59)                       # 23h59m later - still inside 24 hours
        self.assertEqual((self.send()[0], len(self.brevo.accepted)), (1, 300))
        self.at(2026, 10, 15, 10, 1)
        self.assertEqual((self.send()[0], len(self.brevo.accepted)), (0, 600))
        self.at(2026, 10, 16, 10, 5)
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (0, 700))
        self.assertIn("is COMPLETE", out)
        self.at(2026, 10, 17, 10, 5)
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (0, 700))
        self.assertIn("has had the email", out)
        self.assertEqual(sorted(self.brevo.emails()), sorted(self.emails))
        self.assert_no_duplicates_per_round()
        self.assert_never_more_than_limit_in_24_hours()

    def test_next_round_sends_the_next_issue_to_everyone_once(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        for day in (14, 15, 16):
            self.at(2026, 10, day, 10, day - 14)            # a minute later each day (> 24 hours apart)
            self.send()
        self.at(2026, 10, 17, 10, 3)
        self.start_round()
        for day in (17, 18, 19):
            self.at(2026, 10, day, 10, day - 14)
            self.send()
        rounds = Counter(next(t for t in tags if t.startswith("coinedition-round-")) for _, _, tags in self.brevo.accepted)
        self.assertEqual(rounds, Counter({"coinedition-round-2026-10-14": 700, "coinedition-round-2026-10-17": 700}))
        self.assert_no_duplicates_per_round()
        self.assert_never_more_than_limit_in_24_hours()

    def test_crossing_midnight_does_not_reset_the_limit(self):
        self.at(2026, 10, 14, 23, 30)
        self.start_round()
        self.send()
        self.at(2026, 10, 15, 0, 30)                       # a new date, one hour later
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (1, 300))
        self.at(2026, 10, 15, 23, 31)
        self.send()
        self.assertEqual(len(self.brevo.accepted), 600)
        self.assert_never_more_than_limit_in_24_hours()

    def test_brevo_credits_are_respected_test_emails_included(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.extra_used_today = 180                  # e.g. test emails already sent today
        code, out = self.send()
        self.assertEqual(len(self.brevo.accepted), 120)
        self.assertIn("Brevo credits left today: 120", out)

    def test_resplit_with_new_subscribers_cannot_exceed_the_limit(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.send()
        newcomers = [f"new{i:03d}@example-subscriber.test" for i in range(50)]
        write_batches(self.dir / "batches", newcomers + self.emails)  # new sign-ups at the top
        self.at(2026, 10, 14, 16, 0)
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (1, 300))
        self.at(2026, 10, 15, 10, 1)
        self.send()
        self.assertEqual(len(self.brevo.accepted), 600)
        self.assertTrue(set(newcomers) <= set(self.brevo.emails()))
        self.assert_no_duplicates_per_round()
        self.assert_never_more_than_limit_in_24_hours()


class CrashAndLockTests(ProductionTestCase):
    SUBSCRIBERS = 70
    DAILY_LIMIT = 30

    def test_process_dies_right_after_brevo_accepted_no_duplicate(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.kill_after_accepting = 12
        with self.assertRaises(Killed):
            self.send()
        self.assertEqual(self.log().states(self.log().current_round())[self.emails[11]]["state"], "unknown")
        self.brevo.kill_after_accepting = None
        self.at(2026, 10, 14, 10, 5)
        code, out = self.send()
        self.assertIn("1 added to the local log", out)        # Brevo's own record confirmed it
        self.assertEqual(Counter(self.brevo.emails())[self.emails[11]], 1)
        self.assertEqual(len(self.brevo.accepted), 30)
        self.assert_no_duplicates_per_round()

    def test_process_dies_before_brevo_got_it_sent_once_after_24_hours(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.kill_before_accepting = 12
        with self.assertRaises(Killed):
            self.send()
        self.brevo.kill_before_accepting = None
        self.at(2026, 10, 14, 10, 5)
        self.send()                                       # skips it: it MAY have been sent
        self.assertNotIn(self.emails[11], self.brevo.emails())
        self.at(2026, 10, 15, 10, 30)                      # 24 h later, not in Brevo's log -> resend
        code, out = self.send()
        self.assertIn("will be sent again", out)
        self.assertEqual(Counter(self.brevo.emails())[self.emails[11]], 1)
        self.assert_no_duplicates_per_round()
        self.assert_never_more_than_limit_in_24_hours()

    def test_ctrl_c_while_sending_no_duplicate(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.ctrl_c_after_accepting = 7
        code, out = self.send()
        self.assertIn("Stopped with Ctrl+C", out)
        self.brevo.ctrl_c_after_accepting = None
        self.at(2026, 10, 14, 10, 5)
        self.send()
        self.assertEqual(Counter(self.brevo.emails())[self.emails[6]], 1)
        self.assert_no_duplicates_per_round()

    def test_brevo_server_error_is_not_resent_blindly(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.server_error_on = 5                     # Brevo answered 503 - it did NOT accept it
        code, out = self.send()
        self.assertIn("server problem", out)
        self.brevo.server_error_on = None
        self.at(2026, 10, 14, 10, 10)
        self.send()
        self.assertNotIn(self.emails[4], self.brevo.emails())
        self.at(2026, 10, 15, 10, 30)
        self.send()
        self.assertEqual(Counter(self.brevo.emails())[self.emails[4]], 1)

    def test_send_log_open_in_excel_stops_before_sending(self):
        if os.name != "nt":
            self.skipTest("Excel-style file locks are a Windows feature")
        import ctypes
        from ctypes import wintypes
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        handle = k32.CreateFileW(str(self.dir / "logs" / "production_send_log.csv"), 0x80000000, 0x1, None, 3, 0x80, None)
        try:  # opened like Excel does: others may read, nobody may write
            code, out = self.send()
        finally:
            k32.CloseHandle(wintypes.HANDLE(handle))
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("open in another program", out)

    def test_log_becomes_unwritable_mid_run_no_duplicate(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        original, written = send_state.SendLog.append, Counter()

        def flaky(log, round_id, batch, email, status, detail="", when=None):
            if status == "sent":
                written["sent"] += 1
                if written["sent"] == 10:
                    raise send_state.SendLogError("production_send_log.csv is open in another program (Excel?)")
            return original(log, round_id, batch, email, status, detail, when)

        with mock.patch.object(send_state.SendLog, "append", flaky):
            code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (1, 10))
        self.assertIn("won't be re-sent", out)
        self.at(2026, 10, 14, 10, 5)
        self.send()
        self.assertEqual(Counter(self.brevo.emails())[self.emails[9]], 1)
        self.assert_no_duplicates_per_round()

    def test_two_runs_at_the_same_time_are_impossible(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        with send_state.SendLock(self.dir / "logs" / "send.lock"):     # another run holds the lock
            code, out = self.send()
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("another sending run", out)
        self.assertEqual(self.send()[0], 0)                           # free again afterwards


class LostStateTests(ProductionTestCase):
    SUBSCRIBERS = 70
    DAILY_LIMIT = 30

    def sent_one_day(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.send()
        self.assertEqual(len(self.brevo.accepted), 30)

    def test_deleted_log_and_backups_block_everything(self):
        self.sent_one_day()
        (self.dir / "logs" / "production_send_log.csv").unlink()
        for backup in (self.dir / "backups" / "send_log").glob("*.csv"):
            backup.unlink()
        self.at(2026, 10, 15, 10, 30)
        code, out = self.send()
        self.assertEqual(code, 1)
        self.assertIn("No round has been started", out)
        code, out = self.run_cmd("--new-round")
        self.assertEqual(code, 1)
        self.assertIn("Brevo has accepted 30 newsletter emails", out)
        self.assertEqual(len(self.brevo.accepted), 30)

    def test_deleted_log_with_backups_asks_for_a_restore(self):
        self.sent_one_day()
        (self.dir / "logs" / "production_send_log.csv").unlink()
        self.at(2026, 10, 15, 10, 30)
        code, out = self.send()
        self.assertEqual((code, len(self.brevo.accepted)), (1, 30))
        self.assertIn("is missing, but copies of it exist", out)

    def test_older_copy_of_the_log_is_repaired_from_brevo(self):
        self.sent_one_day()
        backups = sorted((self.dir / "backups" / "send_log").glob("*_before.csv"))
        log_file = self.dir / "logs" / "production_send_log.csv"
        log_file.write_bytes(backups[0].read_bytes())      # put back a copy from BEFORE day 1's emails
        self.at(2026, 10, 15, 10, 30)
        code, out = self.send()
        self.assertEqual(code, 0, out)
        self.assertIn("30 added to the local log", out)
        self.assertEqual(len(self.brevo.accepted), 60)
        self.assert_no_duplicates_per_round()
        self.assert_never_more_than_limit_in_24_hours()


class RefusalTests(ProductionTestCase):
    SUBSCRIBERS = 70
    DAILY_LIMIT = 30

    def test_invalid_addresses_do_not_block_the_round(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.reject = {self.emails[3], self.emails[20]}
        for day in (14, 15, 16):
            self.at(2026, 10, day, 10, day - 14)
            self.send()
        code, out = self.run_cmd()
        self.assertIn("is COMPLETE", out)
        self.assertEqual(len(self.brevo.accepted), 68)
        self.assertEqual(self.brevo.calls, 70)                 # each rejected address tried once

    def test_an_address_that_keeps_failing_is_given_up_after_three_tries(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.refuse = {self.emails[4]}
        for day in (14, 15, 16, 17):
            self.at(2026, 10, day, 10, day - 14)
            self.send()
        state = self.log().states(self.log().current_round())[self.emails[4]]
        self.assertEqual((state["state"], state["failures"]), ("rejected", 3))
        self.assertEqual(len(self.brevo.accepted), 69)
        self.assert_no_duplicates_per_round()

    def test_three_refusals_in_a_row_stop_the_run_without_giving_up_on_anyone(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.refuse = set(self.emails[:5])
        code, out = self.send()
        self.assertIn("refused 3 emails in a row", out)
        self.assertEqual((self.brevo.calls, len(self.brevo.accepted)), (3, 0))
        states = self.log().states(self.log().current_round())
        self.assertTrue(all(states[e]["state"] == "failed" for e in self.emails[:3]))


class GateTests(ProductionTestCase):
    SUBSCRIBERS = 70
    DAILY_LIMIT = 30

    def test_old_or_changed_live_template_blocks_sending(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        self.brevo.html = ORIGINAL_TEMPLATE.read_text(encoding="utf-8")   # the upload with {{DATE}} tags
        code, out = self.send()
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("not in Brevo's params format", out)
        self.brevo.html = MASTER_TEMPLATE.read_text(encoding="utf-8").replace("Market Snapshot", "Market snapshot!")
        code, out = self.send()
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("not the same as template/brevo_template_1.html", out)

    def test_sample_or_stale_content_blocks_sending(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        data_file = self.dir / "newsletter" / "newsletter_data.txt"
        write_data_file(data_file, dict(good_data(self.clock().date()), STORY_1_P1="Sample description"))
        code, out = self.send()
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("sample", out)
        write_data_file(data_file, good_data(self.clock().date() - timedelta(days=1)))
        code, out = self.send()
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("READY_TO_SEND_ON", out)

    def test_production_has_no_sample_content_option(self):
        self.at(2026, 10, 14, 10, 0)
        self.start_round()
        write_data_file(self.dir / "newsletter" / "newsletter_data.txt", sample_data(self.clock().date()))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            send_emails.main(["--send", "--allow-sample-content"])        # argparse: unknown option
        self.assertEqual(refused.exception.code, 2)
        code, out = self.send()                                         # the content send_test.py may label
        self.assertEqual((code, self.brevo.calls), (1, 0))
        self.assertIn("READY_TO_SEND_ON is empty", out)
        self.assertIn("GLOBAL_CAP is still sample/placeholder content", out)
        source = Path(send_emails.__file__).read_text(encoding="utf-8")
        self.assertNotIn("check_values", source)                        # only the full check_newsletter_data
        self.assertNotIn("readiness_problems", source)

    def test_status_report_needs_no_internet_and_sends_nothing(self):
        with mock.patch.object(send_emails, "make_client", side_effect=AssertionError("no Brevo call expected")):
            code, out = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertIn("nothing was sent", out)

    def test_test_and_production_build_identical_params(self):
        data, settings = good_data(self.clock().date()), {"sender_name": "Coin Edition", "template_id": 1,
                                                          "sender_email": "noreply@coinedition.com"}
        person = {"email": "a@x.com", "name": "Ana", "surname": "Lee"}
        production = send_emails.build_email(person, settings, data, "2026-10-14", "batch_01", self.clock().date())
        test = send_test.build_email(person, settings, data, self.clock().date())
        self.assertEqual(production["params"], test["params"])
        self.assertEqual((production["templateId"], production["sender"], production["to"]),
                         (test["templateId"], test["sender"], test["to"]))
        self.assertNotEqual(production["tags"], test["tags"])


if __name__ == "__main__":
    unittest.main()
