"""
brevo_api.py - Everything that talks to Brevo, in one place. Used by send_test.py,
send_emails.py and template_sync.py, so tests and production make the same calls.

  load_settings()                .env -> API key (never printed), template id, sender
  BrevoClient                    the Brevo REST API v3:
    account()                    GET  /account                            read-only
    template(id)                 GET  /smtp/templates/{id}                read-only
    senders()                    GET  /senders                            read-only
    requests_count(tag, a, b)    GET  /smtp/statistics/aggregatedReport   read-only
    accepted_emails(tag, a, b)   GET  /smtp/statistics/events             read-only
    send_email(payload)          POST /smtp/email   <- the ONLY call that sends an email

Nothing in this project changes the Brevo template: you paste the HTML into Brevo yourself.
"""

import os
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

from recipients import clean_email, is_valid_email

BREVO_API = "https://api.brevo.com/v3"
REQUEST_TIMEOUT = 30           # seconds to wait for an answer from Brevo

BREVO_ERROR_HINTS = {
    400: "Brevo rejected the request. Usual causes: the template is inactive, the sender address "
         "is not verified in Brevo, or the recipient address is wrong. Brevo's message above says which.",
    401: "The API key was not accepted. Check BREVO_API_KEY in .env - it must be an API key that "
         "starts with 'xkeysib-' (not an SMTP key). You can create one in Brevo: SMTP & API > API Keys.",
    402: "Your Brevo account has no email credits left, or is not activated for sending yet.",
    403: "Brevo blocked the request. If the message mentions an unrecognised IP address, approve it "
         "in Brevo: Security > Authorised IPs (https://app.brevo.com/security/authorised_ips).",
    404: "Not found. Check BREVO_TEMPLATE_ID in .env against the template list in Brevo.",
    429: "Too many requests, or your daily sending limit is used up. Wait and try again later.",
}
# Problems that affect the whole account - no point trying the next address.
ACCOUNT_STATUS_CODES = {401, 402, 403, 404, 429}


class SettingsError(Exception):
    """.env is missing or has a wrong value."""


class BrevoError(Exception):
    """A read-only Brevo request failed (the message explains why, never contains the key)."""


# status: "sent" | "failed" (not sent, may retry) | "rejected" (address refused, never retry)
#         | "unknown" (connection broke - MAY have been sent)
# stop:   a reason to stop the whole run (empty when the next email may follow)
SendResult = namedtuple("SendResult", "status detail stop")


def load_settings(project_dir):
    """Read the Brevo settings from .env. The API key is checked but never printed or logged."""
    env_file = Path(project_dir) / ".env"
    if not env_file.exists():
        raise SettingsError(f"No .env file found in {project_dir}")
    values = dotenv_values(env_file)
    api_key = (values.get("BREVO_API_KEY") or os.getenv("BREVO_API_KEY") or "").strip().strip("\"'")
    template_id = (values.get("BREVO_TEMPLATE_ID") or "").strip()
    sender_email = clean_email(values.get("BREVO_SENDER_EMAIL") or "")
    sender_name = (values.get("BREVO_SENDER_NAME") or "").strip()

    if not api_key or api_key == "PASTE_YOUR_BREVO_API_KEY_HERE":
        raise SettingsError("BREVO_API_KEY is empty. Open .env and paste your Brevo API key after BREVO_API_KEY=")
    if api_key.startswith("xsmtpsib-"):
        raise SettingsError("BREVO_API_KEY is an SMTP key, but this script needs an API key (it starts with "
                            "'xkeysib-'). Create one in Brevo under SMTP & API > API Keys.")
    if not template_id.isdigit():
        raise SettingsError("BREVO_TEMPLATE_ID in .env must be a number, for example: BREVO_TEMPLATE_ID=1")
    if not is_valid_email(sender_email):
        raise SettingsError("BREVO_SENDER_EMAIL in .env is empty or not a valid email address.")
    if not sender_name:
        raise SettingsError("BREVO_SENDER_NAME in .env is empty.")
    return {"api_key": api_key, "template_id": int(template_id),
            "sender_email": sender_email, "sender_name": sender_name}


def read_json(response):
    """The JSON body of a Brevo response as a dict ({} if there is none)."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def explain_brevo_error(response):
    """Turn a failed Brevo response into a short message with a suggested fix."""
    data = read_json(response)
    message = data.get("message") or response.text[:300] or "(no details)"
    code = f" {data['code']}" if data.get("code") else ""
    hint = BREVO_ERROR_HINTS.get(response.status_code)
    if hint is None and response.status_code >= 500:
        hint = "Brevo is having a problem on their side. Wait a few minutes and try again."
    text = f"HTTP {response.status_code}{code}: {message}"
    return f"{text}\n            Fix: {hint}" if hint else text


def network_error_text(error):
    """A short, readable description of a network error (it never contains the API key)."""
    if isinstance(error, requests.exceptions.ConnectionError):
        return "could not connect to api.brevo.com. Check your internet connection, VPN or firewall."
    if isinstance(error, requests.exceptions.Timeout):
        return f"Brevo did not answer within {REQUEST_TIMEOUT} seconds."
    return f"{type(error).__name__}: {str(error)[:200]}"


def daily_credits_left(account):
    """Emails Brevo will still accept today, from GET /account - or None if the plan has no
    daily limit. On the free plan this is Brevo's own counter ("creditsType": "sendLimit"),
    so it cannot be reset by deleting local files or by the clock passing midnight."""
    for plan in account.get("plan", []):
        if plan.get("type") != "sms" and plan.get("creditsType") == "sendLimit":
            try:
                return int(plan.get("credits"))
            except (TypeError, ValueError):
                return 0
    return None


def _is_recipient_error(response):
    """True when a 400 answer is about THIS recipient's address (so the next address may work)."""
    data = read_json(response)
    message = f"{data.get('code', '')} {data.get('message', '')}".lower()
    about_address = ("email is not valid" in message or "invalid email" in message
                     or " in to" in message or "recipient" in message)
    about_setup = any(word in message for word in ("sender", "template", "render", "params", "api key"))
    return about_address and not about_setup


class BrevoClient:
    """A small wrapper around the Brevo REST API. The API key stays inside this object."""

    def __init__(self, api_key):
        self._headers = {"api-key": api_key, "accept": "application/json"}

    # ---- read-only ------------------------------------------------------------------------
    def _get(self, path, **params):
        try:
            response = requests.get(f"{BREVO_API}{path}", headers=self._headers, params=params or None,
                                    timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as error:
            raise BrevoError(f"Could not reach Brevo: {network_error_text(error)}") from None
        if response.status_code != 200:
            raise BrevoError(f"Brevo answered GET {path} with {explain_brevo_error(response)}")
        return read_json(response)

    def account(self):
        return self._get("/account")

    def template(self, template_id):
        return self._get(f"/smtp/templates/{template_id}")

    def senders(self):
        return self._get("/senders").get("senders", [])

    def requests_count(self, tag, start_date, end_date):
        """How many emails with this tag Brevo accepted between two dates (inclusive)."""
        data = self._get("/smtp/statistics/aggregatedReport", tag=tag,
                         startDate=start_date.isoformat(), endDate=end_date.isoformat())
        return int(data.get("requests") or 0)

    def accepted_emails(self, tag, start_date, end_date):
        """Every address Brevo ACCEPTED an email for with this tag between two dates (inclusive),
        from Brevo's own event log -> {email: {"message_id": ..., "date": datetime or None}}.
        Independent of any local file."""
        found, offset, page = {}, 0, 2500
        while True:
            data = self._get("/smtp/statistics/events", tags=tag, event="requests", limit=page, offset=offset,
                             startDate=start_date.isoformat(), endDate=end_date.isoformat())
            events = data.get("events") or []
            for event in events:
                email = clean_email(event.get("email"))
                if not email or email in found:
                    continue
                try:
                    when = datetime.fromisoformat(event.get("date") or "")
                    when = when if when.tzinfo else None
                except ValueError:
                    when = None
                found[email] = {"message_id": event.get("messageId", ""), "date": when}
            if len(events) < page:
                return found
            offset += page

    # ---- the one call that sends ------------------------------------------------------------
    def send_email(self, payload):
        """Send ONE email through Brevo -> SendResult(status, detail, stop).

        "sent"     Brevo accepted it (detail = Brevo's message id)
        "failed"   Brevo refused it, so it was NOT sent; it may be tried again later
        "rejected" Brevo refused this ADDRESS (e.g. not a valid mailbox) - never retried
        "unknown"  the connection broke after the request left this computer (or Brevo
                   answered 5xx), so it MAY have been sent - never sent again automatically
        """
        headers = {**self._headers, "content-type": "application/json"}
        response = None
        for attempt in range(1, 4):
            try:
                response = requests.post(f"{BREVO_API}/smtp/email", headers=headers, json=payload,
                                         timeout=REQUEST_TIMEOUT)
            except requests.exceptions.ConnectTimeout:
                return SendResult("failed", "could not connect to Brevo (timed out) - not sent",
                                  "Brevo could not be reached.")
            except requests.exceptions.RequestException as error:
                return SendResult("unknown", f"connection broke while sending - {network_error_text(error)}",
                                  "The connection to Brevo broke.")
            if response.status_code != 429 or attempt == 3:
                break
            try:
                wait = float(response.headers.get("x-sib-ratelimit-reset", 10))
            except ValueError:
                wait = 10
            wait = min(max(wait, 1), 60)
            print(f"            Brevo asked us to slow down - waiting {wait:.0f} seconds ...")
            time.sleep(wait)

        code = response.status_code
        if code in (200, 201, 202):
            return SendResult("sent", read_json(response).get("messageId", ""), "")
        if code >= 500:
            return SendResult("unknown", explain_brevo_error(response),
                              "Brevo had a server problem - the email MAY have been accepted.")
        if code == 400 and _is_recipient_error(response):
            return SendResult("rejected", explain_brevo_error(response), "")
        if code in ACCOUNT_STATUS_CODES:
            return SendResult("failed", explain_brevo_error(response), "Brevo can't take more emails right now.")
        # Any other refusal: not sent. The run carries on with the next address, and stops
        # if several in a row are refused (then the problem is not about one address).
        return SendResult("failed", explain_brevo_error(response), "")
