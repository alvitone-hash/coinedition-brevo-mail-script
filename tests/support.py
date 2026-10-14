"""
Shared helpers for the tests. Importing this module switches OFF real network access for the
whole test run, so no test can ever reach Brevo or send an email.

Run all tests from the project folder:
    venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import csv
import re
import shutil
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
sys.dont_write_bytecode = True


def _no_network(*args, **kwargs):
    raise RuntimeError("TESTS: real network access is disabled")


socket.getaddrinfo = _no_network
socket.create_connection = _no_network
socket.socket.connect = _no_network

from brevo_api import SendResult  # noqa: E402
from newsletter_params import DATA_KEYS  # noqa: E402

MASTER_TEMPLATE = PROJECT / "template" / "brevo_template_1.html"
ORIGINAL_TEMPLATE = PROJECT / "template" / "newsletter-brevo-original-template.html"
IST = timezone(timedelta(hours=5, minutes=30))


def good_data(today=None):
    """A complete, REAL-looking content set (no sample markers) that passes every check."""
    today = today or date.today()
    data = {key: "" for key in DATA_KEYS}
    data.update({
        "READY_TO_SEND_ON": today.isoformat(),
        "NEWSLETTER_TITLE": "DAILY CRYPTO MARKET RECAP",
        "INTRO_HEADING": "24-Hour Crypto Recap",
        "INTRO_DESCRIPTION": "Here’s what happened across the crypto market over the past 24 hours.",
        "BTC_PRICE": "$84,681.83", "BTC_CHANGE": "-0.6%",
        "ETH_PRICE": "$2,681.55", "ETH_CHANGE": "-1.4%",
        "BNB_PRICE": "$768.21", "BNB_CHANGE": "-0.7%",
        "XRP_PRICE": "$1.49", "XRP_CHANGE": "-0.8%",
        "SOL_PRICE": "$119.34", "SOL_CHANGE": "-0.6%",
        "GLOBAL_CAP": "$2.98T", "GLOBAL_CHANGE": "-0.7%",
        "STORY_1_TITLE": "NEAR Intents Hacker Returns $3.8M",
        "STORY_1_P1": "The attacker behind last week’s NEAR Intents exploit has returned the full **$3.8 million** in stolen funds.",
        "STORY_1_P2": "The investigation has now been closed after the funds were sent back following a 48-hour deadline.",
        "STORY_1_URL": "https://coinedition.com/near-intents-3-8-million-exploit-what-users-should-know-and-do/",
        "STORY_2_TITLE": "SEC Approves 3x Leveraged Crypto ETPs",
        "STORY_2_P1": "The SEC approved **3x long exchange-traded products** linked to:",
        "STORY_2_P2": "**Bitcoin • Ethereum • Gold • Silver • Crude Oil • Natural Gas**",
        "STORY_2_P3": "The development expands the range of leveraged market products available to investors.",
        "STORY_2_URL": "https://coinedition.com/sec-approves-listing-rule-for-3x-bitcoin-and-ether-etfs/",
        "STORY_3_TITLE": "Bitcoin Trades Near $84.6K",
        "STORY_3_P1": "Bitcoin traded around **$84,681**, while the wider crypto market remained under pressure.",
        "STORY_3_URL": "https://coinedition.com/bitcoin-price-live-87k-rejection-puts-79-5k-support-in-focus/",
        "TREND_1_NAME": "Dolphin", "TREND_1_CHANGE": "+141.5%",
        "TREND_2_NAME": "GRX Chain", "TREND_2_CHANGE": "+62.1%",
        "TREND_3_NAME": "Magma Finance", "TREND_3_CHANGE": "+45.0%",
        "TREND_4_NAME": "The Sandbox", "TREND_4_CHANGE": "+77.5%",
        "RADAR_1_LABEL": "BNY × Kraken",
        "RADAR_1_TEXT": "BNY is reportedly in discussions with Kraken parent Payward over a potential partnership.",
        "RADAR_2_LABEL": "Crypto Banking",
        "RADAR_2_TEXT": "U.S. community bankers are challenging national trust bank charters granted to crypto companies.",
        "RADAR_3_LABEL": "Illinois",
        "RADAR_3_TEXT": "The state’s proposed **0.2% crypto transaction tax** has been delayed by six months.",
        "XRP_NEWS_URL": "https://coinedition.com/tag/xrp-news/",
        "INDIA_CRYPTO_NEWS_URL": "https://coinedition.com/tag/india/",
        "CRYPTO_HACKS_URL": "https://coinedition.com/tag/crypto-hacks/",
        "CRYPTO_SCAMS_URL": "https://coinedition.com/tag/crypto-scam/",
        "MICHAEL_SAYLOR_URL": "https://coinedition.com/tag/michael-saylor/",
        "DONALD_TRUMP_URL": "https://coinedition.com/tag/donald-trump/",
        "CHANGPENG_ZHAO_URL": "https://coinedition.com/tag/changpeng-zhao/",
        "VITALIK_BUTERIN_URL": "https://coinedition.com/tag/vitalik-buterin/",
    })
    return data


def sample_data(today=None):
    """Like the real file while it is being prepared: READY_TO_SEND_ON empty and some values still
    marked "(sample)". Every other check passes."""
    return dict(good_data(today), READY_TO_SEND_ON="",
                GLOBAL_CAP="$2.68T (sample - not verified)",
                TREND_1_NAME="Sui (SUI) (sample)",
                RADAR_1_LABEL="US Inflation Data (sample)")


def write_data_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test data\n" + "".join(f"{k}={v}\n" for k, v in data.items()), encoding="utf-8")


def write_batches(batches_dir, emails, size=300):
    batches_dir.mkdir(parents=True, exist_ok=True)
    for old in batches_dir.glob("batch_*.csv"):
        old.unlink()
    for start in range(0, len(emails), size):
        with open(batches_dir / f"batch_{start // size + 1:02d}.csv", "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Email", "Name", "Surname"])
            for email in emails[start:start + size]:
                writer.writerow([email, "Tester", ""])


def make_project(tmp, subscribers, today):
    """A throw-away copy of the project's files: content, master template, fake .env, batches."""
    tmp = Path(tmp)
    write_data_file(tmp / "newsletter" / "newsletter_data.txt", good_data(today))
    (tmp / "template").mkdir(parents=True, exist_ok=True)
    shutil.copy2(MASTER_TEMPLATE, tmp / "template" / "brevo_template_1.html")
    (tmp / ".env").write_text("BREVO_API_KEY=xkeysib-TEST-FAKE-KEY\nBREVO_TEMPLATE_ID=1\n"
                              "BREVO_SENDER_EMAIL=noreply@coinedition.com\nBREVO_SENDER_NAME=Coin Edition\n",
                              encoding="utf-8")
    emails = [f"sub{i:04d}@example-subscriber.test" for i in range(1, subscribers + 1)]
    write_batches(tmp / "batches", emails)
    return emails


class Clock:
    """A clock the tests can move. Times are timezone-aware, like the real ones."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Killed(BaseException):
    """Simulates the process dying (closed window / power cut) - nothing catches it."""


class FakeBrevo:
    """Stands in for brevo_api.BrevoClient. Keeps its own record of every email it accepted,
    like Brevo does, and can be told to misbehave."""

    def __init__(self, clock, html, daily_cap=300):
        self.clock, self.html, self.daily_cap = clock, html, daily_cap
        self.accepted = []          # (time, email, tags)
        self.payloads = []          # the full JSON body of every accepted email
        self.calls = 0              # every send attempt
        self.active = True          # template switched on in Brevo
        self.subject = "Daily Crypto Market Recap"
        self.sender_list = [{"email": "noreply@coinedition.com", "name": "Coin Edition", "active": True}]
        self.reject = set()         # addresses refused as invalid (recipient error)
        self.refuse = set()         # addresses refused for an unclear reason
        self.kill_after_accepting = None   # n-th call: accept, then the process dies
        self.kill_before_accepting = None  # n-th call: the process dies before Brevo gets it
        self.server_error_on = None        # n-th call: HTTP 503 (Brevo did NOT accept it)
        self.ctrl_c_after_accepting = None
        self.extra_used_today = 0          # e.g. test emails sent from the same account

    # read-only calls
    def account(self):
        today = self.clock().date()
        used = sum(1 for t, _, _ in self.accepted if t.date() == today) + self.extra_used_today
        return {"plan": [{"type": "free", "creditsType": "sendLimit", "credits": max(self.daily_cap - used, 0)}]}

    def template(self, template_id):
        return {"id": template_id, "name": "Coin Edition Daily Recap", "subject": self.subject,
                "isActive": self.active, "htmlContent": self.html}

    def senders(self):
        return self.sender_list

    def requests_count(self, tag, start_date, end_date):
        return sum(1 for t, _, tags in self.accepted if tag in tags and start_date <= t.date() <= end_date)

    def accepted_emails(self, tag, start_date, end_date):
        found = {}
        for i, (t, e, tags) in enumerate(self.accepted):
            if tag in tags and start_date <= t.date() <= end_date and e not in found:
                found[e] = {"message_id": f"<msg-{i}>", "date": t}
        return found

    # the send
    def send_email(self, payload):
        self.calls += 1
        email = payload["to"][0]["email"]
        if self.kill_before_accepting == self.calls:
            raise Killed()
        if self.server_error_on == self.calls:
            return SendResult("unknown", "HTTP 503: Service Unavailable", "Brevo had a server problem.")
        if email in self.reject:
            return SendResult("rejected", "HTTP 400 invalid_parameter: email is not valid in to", "")
        if email in self.refuse:
            return SendResult("failed", "HTTP 400 bad_request: something odd", "")
        self.accepted.append((self.clock(), email, list(payload.get("tags", []))))
        self.payloads.append(payload)
        if self.kill_after_accepting == self.calls:
            raise Killed()
        if self.ctrl_c_after_accepting == self.calls:
            raise KeyboardInterrupt()
        return SendResult("sent", f"<msg-{self.calls}>", "")

    # helpers for assertions
    def emails(self):
        return [e for _, e, _ in self.accepted]

    def per_day(self):
        days = {}
        for t, _, _ in self.accepted:
            days[t.date()] = days.get(t.date(), 0) + 1
        return days


def auto_answer(question):
    """The operator always types what the prompt asks for (the text in brackets)."""
    match = re.search(r"\(([^)]+)\)", question)
    return match.group(1) if match else ""


def ist(*args):
    return datetime(*args, tzinfo=IST)
