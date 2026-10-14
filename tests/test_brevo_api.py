"""How every possible Brevo answer to a send is classified (no network: requests.post is faked)."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

import support  # noqa: F401

import brevo_api


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def send_with(*answers):
    """Run BrevoClient.send_email while requests.post returns/raises the given answers in turn."""
    answers = list(answers)

    def fake_post(*args, **kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with mock.patch.object(brevo_api.requests, "post", side_effect=fake_post) as post, \
            mock.patch.object(brevo_api.time, "sleep"), redirect_stdout(io.StringIO()):
        result = brevo_api.BrevoClient("xkeysib-TEST").send_email({"to": [{"email": "a@x.com"}]})
    return result, post.call_count


class SendClassificationTests(unittest.TestCase):
    def test_accepted(self):
        result, _ = send_with(FakeResponse(201, {"messageId": "<abc@smtp>"}))
        self.assertEqual(result, ("sent", "<abc@smtp>", ""))

    def test_invalid_address_is_rejected_and_the_run_continues(self):
        result, _ = send_with(FakeResponse(400, {"code": "invalid_parameter", "message": "email is not valid in to"}))
        self.assertEqual((result.status, result.stop), ("rejected", ""))

    def test_unclear_400_is_failed_but_not_permanent(self):
        result, _ = send_with(FakeResponse(400, {"code": "invalid_parameter", "message": "Template Render Error"}))
        self.assertEqual((result.status, result.stop), ("failed", ""))
        result, _ = send_with(FakeResponse(400, {"code": "invalid_parameter", "message": "sender email is not valid"}))
        self.assertEqual(result.status, "failed")

    def test_account_problems_stop_the_run(self):
        for status in (401, 402, 403, 404):
            result, _ = send_with(FakeResponse(status, {"message": "x"}))
            self.assertEqual(result.status, "failed", status)
            self.assertTrue(result.stop, status)

    def test_rate_limit_is_retried_then_stops(self):
        result, calls = send_with(FakeResponse(429, headers={"x-sib-ratelimit-reset": "2"}),
                                  FakeResponse(429), FakeResponse(429))
        self.assertEqual((result.status, calls), ("failed", 3))
        self.assertTrue(result.stop)
        result, calls = send_with(FakeResponse(429), FakeResponse(201, {"messageId": "<m>"}))
        self.assertEqual((result.status, calls), ("sent", 2))

    def test_server_errors_count_as_maybe_sent(self):
        for status in (500, 502, 503, 504):
            result, _ = send_with(FakeResponse(status))
            self.assertEqual(result.status, "unknown", status)
            self.assertTrue(result.stop)

    def test_network_problems(self):
        result, _ = send_with(requests.exceptions.ConnectTimeout("x"))
        self.assertEqual(result.status, "failed")       # never reached Brevo
        result, _ = send_with(requests.exceptions.ReadTimeout("x"))
        self.assertEqual(result.status, "unknown")      # Brevo may have it
        result, _ = send_with(requests.exceptions.ConnectionError("reset"))
        self.assertEqual(result.status, "unknown")


class AccountAndSettingsTests(unittest.TestCase):
    def test_daily_credits(self):
        self.assertEqual(brevo_api.daily_credits_left(
            {"plan": [{"type": "free", "credits": 296, "creditsType": "sendLimit"}]}), 296)
        self.assertIsNone(brevo_api.daily_credits_left({"plan": [{"type": "payAsYouGo", "credits": 5000,
                                                                  "creditsType": "credits"}]}))

    def test_settings_errors_never_show_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("BREVO_API_KEY=xsmtpsib-SECRET123\nBREVO_TEMPLATE_ID=1\n"
                                         "BREVO_SENDER_EMAIL=noreply@coinedition.com\nBREVO_SENDER_NAME=CE\n")
            with self.assertRaises(brevo_api.SettingsError) as error:
                brevo_api.load_settings(tmp)
        self.assertNotIn("SECRET123", str(error.exception))

    def test_the_project_never_changes_the_template(self):
        self.assertFalse(hasattr(brevo_api.BrevoClient, "update_template_html"))
        source = Path(brevo_api.__file__).read_text(encoding="utf-8")
        self.assertNotIn("requests.put", source)
        self.assertEqual(source.count("requests.post("), 1)  # the single send call


if __name__ == "__main__":
    unittest.main()
