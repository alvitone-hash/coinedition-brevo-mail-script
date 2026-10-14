"""send_test.py - the 4 TEST addresses only, and the TEST-ONLY --allow-sample-content option.

Each test works in a temporary copy of the project with a FAKE Brevo (tests/support.py). Real
network access is switched off for the whole test run, so nothing can ever be sent.
"""

import csv
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from support import MASTER_TEMPLATE, Clock, FakeBrevo, good_data, make_project, sample_data, write_data_file

import newsletter_params as np
import send_test
from send_state import now

# (Email, Name) - like tests/test_recipients.csv, with addresses that can't exist
RECIPIENTS = [("ana@test-recipients.test", "Ana"), ("ben@test-recipients.test", "Ben"),
              ("cara@test-recipients.test", "Cara"), ("dev@test-recipients.test", "Dev")]


def write_recipients(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Name", "Surname"])
        for email, name in rows:
            writer.writerow([email, name, ""])


class TestSendCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.today = date.today()
        make_project(self.dir, 0, self.today)
        self.data_file = self.dir / "newsletter" / "newsletter_data.txt"
        write_data_file(self.data_file, sample_data(self.today))
        self.recipients_file = self.dir / "tests" / "test_recipients.csv"
        write_recipients(self.recipients_file, RECIPIENTS)
        self.master = self.dir / "template" / "brevo_template_1.html"
        self.brevo = FakeBrevo(Clock(now()), MASTER_TEMPLATE.read_text(encoding="utf-8"))
        self.answer, self.questions = "YES", []
        logs = self.dir / "logs"
        self.patches = [
            mock.patch.object(send_test, "PROJECT_DIR", self.dir),
            mock.patch.object(send_test, "DATA_FILE", self.data_file),
            mock.patch.object(send_test, "TEST_RECIPIENTS_FILE", self.recipients_file),
            mock.patch.object(send_test, "TEST_LOG_FILE", logs / "test_send_log.csv"),
            mock.patch.object(send_test, "PAYLOAD_FILE", logs / "dry_run_payload.json"),
            mock.patch.object(send_test, "PREVIEW_FILE", logs / "dry_run_preview.html"),
            mock.patch.object(send_test, "MASTER_TEMPLATE", self.master),
            mock.patch.object(send_test, "LOCK_FILE", logs / "send.lock"),
            mock.patch.object(send_test, "BrevoClient", lambda key: self.brevo),
            mock.patch.object(send_test, "PAUSE_BETWEEN_EMAILS", 0),
            mock.patch.object(send_test, "ask", lambda question: self.questions.append(question) or self.answer),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self._tmp.cleanup()

    def run_test_send(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            try:
                code = send_test.main(list(args))
            except SystemExit as stopped:  # stop() and argparse errors
                code = stopped.code
        return code, output.getvalue()

    def assert_nothing_sent(self, code, out):
        self.assertEqual((code, self.brevo.calls), (1, 0), out)


class NormalTestSendTests(TestSendCase):
    def test_unverified_content_blocks_a_normal_test_send(self):
        for args in ([], ["--dry-run"]):
            with self.subTest(args=args):
                code, out = self.run_test_send(*args)
                self.assert_nothing_sent(code, out)
                self.assertIn("[FAIL] READY_TO_SEND_ON is empty", out)
                self.assertIn("[FAIL] GLOBAL_CAP is still sample/placeholder content", out)
                self.assertIn("python send_test.py --dry-run --allow-sample-content", out)   # the hint

    def test_confirmed_content_goes_to_the_four_test_addresses_personalised(self):
        write_data_file(self.data_file, good_data(self.today))
        code, out = self.run_test_send()
        self.assertEqual(code, 0, out)
        self.assertEqual([p["to"] for p in self.brevo.payloads], [[{"email": e, "name": n}] for e, n in RECIPIENTS])
        self.assertEqual([p["params"]["first_name"] for p in self.brevo.payloads], [n for _, n in RECIPIENTS])
        for payload in self.brevo.payloads:
            self.assertEqual((payload["templateId"], payload["tags"]), (1, [send_test.BREVO_TAG]))
            self.assertEqual(payload["subject"], "NEAR Intents Hacker Returns $3.8M")   # STORY_1_TITLE, as production
            self.assertEqual(payload["params"]["date"], np.format_date(self.today))
        self.assertNotIn("UNVERIFIED", out)

    def test_every_param_comes_from_the_data_file(self):
        write_data_file(self.data_file, good_data(self.today))
        self.run_test_send()
        data, _ = np.read_newsletter_data(self.data_file)
        for payload, (_, name) in zip(self.brevo.payloads, RECIPIENTS):
            self.assertEqual(payload["params"], np.build_params(data, name, self.today))
        self.assertEqual(len(self.brevo.payloads[0]["params"]), 54)

    def test_an_edited_value_is_used_by_the_next_test_email(self):
        write_data_file(self.data_file, good_data(self.today))
        self.assertEqual(self.run_test_send()[0], 0)
        write_data_file(self.data_file, dict(good_data(self.today), BTC_PRICE="$90,000.00"))
        self.assertEqual(self.run_test_send()[0], 0)
        self.assertEqual([p["params"]["btc_price"] for p in self.brevo.payloads],
                         ["$84,681.83"] * 4 + ["$90,000.00"] * 4)

    def test_test_email_is_exactly_the_production_email(self):
        write_data_file(self.data_file, dict(good_data(self.today), STORY_1_TITLE="Ledger Confirms Tampered Hardware Wallets"))
        code, out = self.run_test_send()
        self.assertEqual(code, 0, out)
        self.assertEqual({p["subject"] for p in self.brevo.payloads}, {"Ledger Confirms Tampered Hardware Wallets"})
        self.assertIn("Subject  : Ledger Confirms Tampered Hardware Wallets", out)
        for payload in self.brevo.payloads:   # no test label anywhere in what Brevo shows the reader
            shown = json.dumps({"subject": payload["subject"], "params": payload["params"]}, ensure_ascii=False)
            self.assertIsNone(re.search(r"\btest\b", shown, re.IGNORECASE), shown)

    def test_addresses_are_sent_exactly_as_written_and_an_invalid_one_is_still_tried(self):
        with open(self.recipients_file, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["Email", "Name", "Surname"], ["Ana.Lima@Example-Test.com", "Ana", ""],
                                     ["ben@example-test.com", "Ben", ""], ["carla.@example-test.com", "Carla", ""],
                                     ["dev@example-test.com", "Dev", ""]])
        write_data_file(self.data_file, good_data(self.today))
        code, out = self.run_test_send()
        self.assertEqual(code, 0, out)
        self.assertEqual([p["to"][0]["email"] for p in self.brevo.payloads],
                         ["Ana.Lima@Example-Test.com", "ben@example-test.com", "carla.@example-test.com",
                          "dev@example-test.com"])
        self.assertIn("'carla.@example-test.com' does not look like a valid email address - it is still tried", out)


class SampleContentOptionTests(TestSendCase):
    def test_labelled_test_emails_go_to_the_four_test_addresses_only(self):
        code, out = self.run_test_send("--allow-sample-content")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.brevo.emails(), [e for e, _ in RECIPIENTS])
        for payload, (email, name) in zip(self.brevo.payloads, RECIPIENTS):
            params = payload["params"]
            self.assertEqual(payload["to"], [{"email": email, "name": name}])
            self.assertEqual(params["first_name"], name)
            self.assertEqual((payload["templateId"], payload["tags"]), (1, [send_test.BREVO_TAG]))
            self.assertEqual(payload["subject"], "[TEST – UNVERIFIED SAMPLE CONTENT] NEAR Intents Hacker Returns $3.8M")
            self.assertEqual(params["date"], f"TEST EMAIL – UNVERIFIED SAMPLE CONTENT · {np.format_date(self.today)}")
            self.assertTrue(params["intro_description"].startswith("This is a TEST email and its content has NOT"))
            self.assertEqual(params["global_cap"], "$2.68T (sample - not verified)")   # shown as written
        self.assertIn("[SAMPLE] READY_TO_SEND_ON is empty", out)
        self.assertIn("[SAMPLE] TREND_1_NAME is still sample/placeholder content", out)
        self.assertFalse((self.dir / "logs" / "production_send_log.csv").exists())
        self.assertEqual(len((self.dir / "logs" / "test_send_log.csv").read_text(encoding="utf-8").splitlines()), 5)

    def test_only_the_labels_differ_from_the_data_file(self):
        self.run_test_send("--allow-sample-content")
        data, _ = np.read_newsletter_data(self.data_file)
        expected = np.build_params(data, "Ana", self.today)
        sent = self.brevo.payloads[0]["params"]
        self.assertEqual(set(sent), set(expected))                      # the same 54 params
        self.assertEqual({k for k in expected if sent[k] != expected[k]}, {"date", "intro_description"})
        self.assertTrue(sent["intro_description"].endswith(expected["intro_description"]))

    def test_still_asks_for_yes(self):
        for answer in ("", "yes please", "Y"):
            with self.subTest(answer=answer):
                self.answer = answer
                code, out = self.run_test_send("--allow-sample-content")
                self.assertEqual((code, self.brevo.calls), (0, 0))
                self.assertIn("Cancelled - nothing was sent", out)
        self.assertIn("Type YES to send 4 test emails with UNVERIFIED sample content", self.questions[0])

    def test_dry_run_sends_nothing_and_shows_the_labels(self):
        code, out = self.run_test_send("--dry-run", "--allow-sample-content")
        self.assertEqual((code, self.brevo.calls), (0, 0), out)
        self.assertIn("READY FOR A LABELLED TEST", out)
        self.assertIn("To send the test for real, run:  python send_test.py --allow-sample-content", out)
        preview = (self.dir / "logs" / "dry_run_preview.html").read_text(encoding="utf-8")
        self.assertIn(send_test.SAMPLE_DATE_LABEL, preview)
        saved = json.loads((self.dir / "logs" / "dry_run_payload.json").read_text(encoding="utf-8"))
        self.assertTrue(all(p["subject"].startswith(send_test.SAMPLE_SUBJECT_PREFIX) for p in saved["payloads"]))

    def test_confirmed_content_is_not_labelled(self):
        write_data_file(self.data_file, good_data(self.today))
        code, out = self.run_test_send("--allow-sample-content")
        self.assertEqual(code, 0, out)
        for payload in self.brevo.payloads:
            self.assertEqual(payload["subject"], "NEAR Intents Hacker Returns $3.8M")   # no label at all
            self.assertEqual(payload["params"]["date"], np.format_date(self.today))
        self.assertNotIn("[SAMPLE]", out)

    def test_every_other_content_check_still_blocks(self):
        for change, message in [({"STORY_1_URL": "http://coinedition.com/x"}, "must start with https://"),
                                ({"STORY_2_URL": "https://example.com/x"}, "is a placeholder address"),
                                ({"BTC_PRICE": ""}, "BTC_PRICE is required"),
                                ({"STORY_1_P1": "<b>Big</b> news"}, "contains the HTML tag"),
                                ({"RADAR_1_TEXT": "the **$3.8 million"}, "odd number of **"),
                                ({"SOL_CHANGE": "0.92%"}, "needs a + or - sign"),
                                ({"TREND_2_CHANGE": "3.0%"}, "TREND_2_CHANGE=3.0% needs a + or - sign"),
                                ({"BNB_COLOR": "green"}, "disagrees with BNB_CHANGE")]:
            with self.subTest(change=change):
                write_data_file(self.data_file, dict(sample_data(self.today), **change))
                code, out = self.run_test_send("--allow-sample-content")
                self.assert_nothing_sent(code, out)
                self.assertIn(message, out)

    def test_the_live_template_must_still_match_the_master_copy(self):
        self.brevo.html = MASTER_TEMPLATE.read_text(encoding="utf-8").replace("Market Snapshot", "Market snapshot!")
        for args in (["--allow-sample-content"], ["--dry-run", "--local-template", "--allow-sample-content"]):
            with self.subTest(args=args):
                code, out = self.run_test_send(*args)
                self.assert_nothing_sent(code, out)
                self.assertIn("the LIVE template in Brevo is not template/brevo_template_1.html", out)

    def test_template_and_sender_checks_still_block(self):
        self.brevo.active = False
        code, out = self.run_test_send("--allow-sample-content")
        self.assert_nothing_sent(code, out)
        self.assertIn("is INACTIVE in Brevo", out)
        self.brevo.active = True
        self.brevo.sender_list = []
        code, out = self.run_test_send("--allow-sample-content")
        self.assert_nothing_sent(code, out)
        self.assertIn("is not in your Brevo senders list", out)

    def test_recipient_rules_still_apply(self):
        for rows, message in [(RECIPIENTS[:3], "it must have exactly 4"),
                              (RECIPIENTS[:3] + [("test1@gmail.com", "X")], "is an example address"),
                              (RECIPIENTS[:3] + [RECIPIENTS[0]], "is listed twice")]:
            with self.subTest(message=message):
                write_recipients(self.recipients_file, rows)
                code, out = self.run_test_send("--allow-sample-content")
                self.assert_nothing_sent(code, out)
                self.assertIn(message, out)

    def test_brevo_credits_are_still_checked(self):
        self.brevo.extra_used_today = 298                               # 2 credits left, 4 needed
        code, out = self.run_test_send("--allow-sample-content")
        self.assert_nothing_sent(code, out)
        self.assertIn("not enough for 4", out)


if __name__ == "__main__":
    unittest.main()
