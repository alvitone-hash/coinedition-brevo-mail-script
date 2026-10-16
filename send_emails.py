"""
send_emails.py - PRODUCTION sender for the real subscribers.

        *** SWITCHED OFF BY DEFAULT - it cannot send anything until you turn it on. ***

The plan
--------
    data/<subscriber file>.csv or .xlsx
      -> python split_excel.py              makes batches/batch_01.csv, batch_02.csv, ... (300 each)
      -> python send_emails.py --new-round  starts a ROUND: everybody in batches/ will get ONE email
      -> python send_emails.py --send       once a day: emails the next people who haven't had this
                                            round's email yet - never more than 300 in 24 hours
      -> when everybody has had it:         python send_emails.py --new-round   (the next issue)

    The newsletter content (prices, stories, article URLs ...) comes from
    newsletter/newsletter_data.txt, read fresh on every run, and is turned into Brevo params by
    newsletter_params.py - the same code send_test.py uses, so the test shows what production sends.

How duplicates are prevented
----------------------------
  * logs/production_send_log.csv records every email of the round. A "pending" line is saved to
    disk BEFORE each email is handed to Brevo, so a crash, a closed window, a power cut or a log
    file left open in Excel can never make the script email that person again (see send_state.py).
  * Before every run the script also asks Brevo which addresses it already accepted in this
    round (Brevo's own event log) and skips them - so a lost or restored log can't cause repeats.
  * If Brevo's records show more newsletter emails than the log knows about, nothing is sent.
  * Only one sending run at a time (logs/send.lock - released automatically if the script dies).
  * Each round is separate, so last round's emails never stop the next issue from going out.

How the 300-a-day limit is kept
-------------------------------
  * It counts EMAILS sent (or possibly sent) in the last 24 hours - not batches, not calendar
    days - so crossing midnight, re-splitting or new subscribers can't add more.
  * And never more than Brevo's own "credits left today" (GET /account), which Brevo counts
    itself: it includes test emails and can't be reset by deleting local files.

Safety locks - ALL of them must pass before a single email is sent
------------------------------------------------------------------
  1. PRODUCTION_SENDING_ENABLED (below) must be changed from False to True.
  2. You must add --send. Without it the script only prints a status report.
  3. No other sending run may be active, and the send log must be readable and writable.
  4. newsletter/newsletter_data.txt must pass every check, including READY_TO_SEND_ON = today,
     so sample content or yesterday's prices can't go out by mistake. There is no override here
     (send_test.py --allow-sample-content is for the test address only).
  5. The live Brevo template must pass every check and match template/brevo_template_1.html.
  6. Brevo's records must agree with the local send log.
  7. Today's allowance (see above) must be more than 0.
  8. You must type the number of emails to confirm.

Usage
-----
    python send_emails.py               status report only - sends nothing, no internet needed
    python send_emails.py --new-round   start a new round (sends nothing)
    python send_emails.py --send        send today's portion (only if every safety lock passes)
    python send_emails.py --send --max 280
                                        the same, but at most 280 emails in this run
    python send_emails.py --send --dry-run [--max 280]
                                        everything --send checks, READ-ONLY: lists who would get the
                                        email now and saves them in logs/. Sends nothing, writes nothing
                                        to the send log, and works while sending is switched off.

Email addresses are used exactly as written in the batch files - never tidied, fixed or skipped.
"""

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from brevo_api import BrevoClient, BrevoError, SettingsError, daily_credits_left, load_settings
from newsletter_params import (build_params, check_newsletter_data, email_subject, entries_report, params_table,
                               read_newsletter_data)
from recipients import find_column, read_table
from send_state import (DONE, GIVE_UP_AFTER, LockedError, SendLock, SendLog, SendLogError, backup_log, now,
                        round_tag)
from template_check import check_rendered, check_template, render


# =============================================================================
# SAFETY SWITCH - leave this False until testing with send_test.py is finished
# and you are ready to email real subscribers.
# =============================================================================
PRODUCTION_SENDING_ENABLED = False


# =============================================================================
# SETTINGS
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent

DAILY_LIMIT = 300              # Brevo's free plan: 300 emails a day, test emails included
PAUSE_BETWEEN_EMAILS = 0.5     # seconds
MAX_FAILURES_IN_A_ROW = 3      # Brevo refused several in a row -> not one address's fault: stop
PRODUCTION_TAG = "coinedition-newsletter"  # on every production email (test emails use another tag)
HISTORY_DAYS = 30              # how far back Brevo's records are compared with the send log
UNKNOWN_RETRY_AFTER = timedelta(hours=24)  # "unknown" + not in Brevo's event log after this -> resend
DEFAULT_FIRST_NAME = "there"   # sent as {{ params.first_name }} when a subscriber has no Name

# Replaced by the tests; the real ones are used otherwise.
make_client = BrevoClient
clock = now
sleep = time.sleep


def ask(question):
    """Ask a question in the terminal. Returns "" if nobody can answer (e.g. a scheduled task)."""
    try:
        return input(question).strip()
    except EOFError:
        return ""


class Paths:
    """Every file and folder the script uses."""

    def __init__(self, project_dir):
        project_dir = Path(project_dir)
        self.batches = project_dir / "batches"
        self.data_file = project_dir / "newsletter" / "newsletter_data.txt"
        self.master_template = project_dir / "template" / "brevo_template_1.html"
        self.env_dir = project_dir
        self.send_log = project_dir / "logs" / "production_send_log.csv"
        self.lock = project_dir / "logs" / "send.lock"
        self.log_backups = project_dir / "backups" / "send_log"
        self.dry_run_list = project_dir / "logs" / "production_dry_run_recipients.csv"
        self.dry_run_example = project_dir / "logs" / "production_dry_run_example.json"
        self.dry_run_preview = project_dir / "logs" / "production_dry_run_preview.html"


class Stop(Exception):
    """Ends the run BEFORE any email of this run is sent, with a message for the user."""


# =============================================================================
# READING THE BATCHES AND THE LOG
# =============================================================================

def read_batches(batches_dir):
    """[(batch name, [person, ...]), ...] in number order. Every address is used exactly as written in the
    batch file - never changed or skipped. One that Brevo refuses is logged as rejected, and the run
    goes on with the others. Only rows with no address at all are left out."""
    numbered = []
    for path in Path(batches_dir).glob("batch_*.csv"):
        number = path.stem[len("batch_"):]
        if number.isdigit():
            numbered.append((int(number), path))
    batches = []
    for _, path in sorted(numbered):
        try:
            headers, rows, _ = read_table(path)
        except (OSError, ValueError) as error:
            raise Stop(f"Could not read {path.name}: {error}") from None
        email_column = find_column(headers, "email")
        if not email_column:
            raise Stop(f"{path.name} has no 'Email' column.")
        name_column, surname_column = find_column(headers, "name"), find_column(headers, "surname")
        people = []
        for row in rows:
            email = str(row.get(email_column) or "")  # exactly as written - never tidied, fixed or dropped
            if email.strip():
                people.append({"email": email,
                               "name": (row.get(name_column) or "").strip() if name_column else "",
                               "surname": (row.get(surname_column) or "").strip() if surname_column else ""})
        batches.append((path.stem, people))
    return batches


def address_key(email):
    """How the send log recognises an address: ignoring capitals and surrounding spaces. The address
    itself is always sent and logged exactly as written."""
    return email.strip().lower()


def everyone(batches, states):
    """[(batch, person, state)] for everybody, in batch order. An address that appears in two
    batches (after a re-split) is listed once."""
    seen, result = set(), []
    for batch, people in batches:
        for person in people:
            key = address_key(person["email"])
            if key not in seen:
                seen.add(key)
                result.append((batch, person, states.get(key, {})))
    return result


def open_log(paths):
    """Load the send log - stopping if it is missing while backups exist, damaged, or locked."""
    backups = sorted(paths.log_backups.glob("production_send_log_*.csv")) if paths.log_backups.exists() else []
    if not paths.send_log.exists() and backups:
        raise Stop("logs/production_send_log.csv is missing, but copies of it exist in backups/send_log/ "
                   f"(newest: {backups[-1].name}). It must never be deleted: copy the newest backup to "
                   "logs/production_send_log.csv and run again.")
    try:
        log = SendLog(paths.send_log)
        log.check_writable()
    except SendLogError as error:
        raise Stop(str(error)) from None
    return log


# =============================================================================
# BREVO CHECKS
# =============================================================================

def get_settings(paths):
    try:
        return load_settings(paths.env_dir)
    except SettingsError as error:
        raise Stop(str(error)) from None


def brevo(call, *args):
    """Run a read-only Brevo request; any problem stops the run before anything is sent."""
    try:
        return call(*args)
    except BrevoError as error:
        raise Stop(str(error)) from None


def verify_template(client, settings, data, paths):
    """The live template must be active, pass every check, and equal the local master copy."""
    template = brevo(client.template, settings["template_id"])
    html = template.get("htmlContent", "")
    params = build_params(data, "Subscriber", clock().date())
    problems, warnings = check_template(html, params)
    try:
        problems += check_rendered(render(html, params))
    except Exception as error:  # a tag the preview can't draw
        problems.append(f"the template could not be rendered: {error}")
    if template.get("isActive") is False:
        problems.append(f"template {settings['template_id']} is INACTIVE in Brevo")
    if not paths.master_template.exists():
        problems.append("template/brevo_template_1.html (the local master copy) is missing - "
                        "run: python template_sync.py pull")
    elif paths.master_template.read_text(encoding="utf-8").replace("\r\n", "\n") != html.replace("\r\n", "\n"):
        problems.append("the LIVE template in Brevo is not the same as template/brevo_template_1.html - someone "
                        "changed one of them. Check with: python template_sync.py status")
    if problems:
        raise Stop(f"Brevo template {settings['template_id']} is not ready:\n    " + "\n    ".join(problems))
    print(f"  [OK]   Template {settings['template_id']} \"{template.get('name', '')}\": active, every placeholder "
          "matches the params, links and unsubscribe checked, same as the local master copy")
    for warning in warnings:
        print(f"  [WARN] {warning}")


def reconcile_with_brevo(client, log, round_id, today):
    """Add every address Brevo already accepted in this round (from Brevo's own event log) to the
    log, and give 'unknown' addresses that Brevo never received another chance after 24 hours."""
    started = log.round_started(round_id).date()
    accepted = brevo(client.accepted_emails, round_tag(round_id), started - timedelta(days=1), today)
    states = log.states(round_id)
    added = 0
    for email, event in accepted.items():
        if states.get(email, {}).get("state") != "sent":
            # Recorded at the time Brevo accepted it, so the 24-hour count stays right.
            when = min(event["date"], clock()) if event.get("date") else clock()
            log.append(round_id, states.get(email, {}).get("batch", ""), email, "sent",
                       f"confirmed in Brevo's event log ({event.get('message_id', '')})", when=when.replace(microsecond=0))
            added += 1
    retried = 0
    for email, state in log.states(round_id).items():
        attempt = state["last_attempt"]
        if state["state"] == "unknown" and attempt and clock() - attempt >= UNKNOWN_RETRY_AFTER:
            log.append(round_id, state["batch"], email, "failed",
                       "not in Brevo's event log 24 hours after the attempt, so it was not sent - will be sent again")
            retried += 1
    print(f"  [OK]   Brevo's event log for this round checked: {len(accepted)} accepted"
          + (f", {added} added to the local log" if added else "")
          + (f", {retried} 'unknown' address(es) not received by Brevo will be sent again" if retried else ""))


def check_history(client, log, today):
    """Stop if Brevo has accepted more newsletter emails than the local log records."""
    first = today - timedelta(days=HISTORY_DAYS - 1)
    in_brevo = brevo(client.requests_count, PRODUCTION_TAG, first, today)
    in_log = log.accepted_between_days(first - timedelta(days=1), today + timedelta(days=1))
    if in_brevo > in_log:
        raise Stop(f"Brevo has accepted {in_brevo} newsletter emails since {first}, but "
                   f"logs/production_send_log.csv only records {in_log}. The log may have been deleted, moved, "
                   "replaced by an older copy or damaged, so the script can't be sure who already has the email. "
                   "Restore the newest copy from backups/send_log/ (or the original file) and run again.")
    print(f"  [OK]   Brevo's records agree with the send log ({in_brevo} newsletter emails in the last {HISTORY_DAYS} days)")


def give_up_on_failing_addresses(log, round_id):
    """Addresses Brevo refused GIVE_UP_AFTER times while other emails went through -> "rejected"."""
    for email, state in log.states(round_id).items():
        if state["state"] == "failed" and state["counted_failures"] >= GIVE_UP_AFTER:
            log.append(round_id, state["batch"], email, "rejected",
                       f"gave up after {state['failures']} refusals while other emails went through "
                       f"(last: {state['detail'][:150]})")
            print(f"  [INFO] Gave up on {email} - Brevo refused it {state['failures']} times")


# =============================================================================
# SENDING
# =============================================================================

def build_email(person, settings, data, round_id, batch, today):
    """The JSON body for ONE email, in the format Brevo's /v3/smtp/email expects."""
    first_name = person["name"] or DEFAULT_FIRST_NAME
    full_name = " ".join(part for part in (person["name"], person["surname"]) if part)
    to = {"email": person["email"]}
    if full_name:
        to["name"] = full_name
    return {
        "sender": {"name": settings["sender_name"], "email": settings["sender_email"]},
        "to": [to],  # one recipient per email, so nobody sees the other addresses
        "templateId": settings["template_id"],
        "subject": email_subject(data),  # the first Top Stories heading, instead of the template's subject
        "params": build_params(data, first_name, today),  # same code as the test
        "tags": [PRODUCTION_TAG, round_tag(round_id), batch],
    }


def send_queue(client, settings, data, log, round_id, queue, today):
    """Send to everybody in the queue. Returns (counts, why it stopped early or "")."""
    counts, failures_in_a_row, stop_reason, in_flight = Counter(), 0, "", None
    try:
        for number, (batch, person) in enumerate(queue, start=1):
            email = person["email"]
            payload = build_email(person, settings, data, round_id, batch, today)
            try:
                log.append(round_id, batch, email, "pending")   # on disk BEFORE Brevo gets it
            except SendLogError as error:
                stop_reason = f"Stopped before emailing {email}: {error}."
                break
            in_flight = (batch, email)
            result = client.send_email(payload)
            in_flight = None
            counts[result.status] += 1
            try:
                log.append(round_id, batch, email, result.status, result.detail)
            except SendLogError as error:
                stop_reason = (f"Brevo answered '{result.status}' for {email}, but that could not be written to "
                               f"the send log ({error}). It counts as 'unknown' and won't be re-sent. Stopped.")
                break
            print(f"  [{number:>3}/{len(queue)}] {result.status.upper():<8} {email}")
            if result.status != "sent":
                print(f"            {result.detail}")
            failures_in_a_row = failures_in_a_row + 1 if result.status == "failed" else 0
            if result.stop:
                stop_reason = f"Stopped: {result.stop}"
                break
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                stop_reason = (f"Stopped: Brevo refused {MAX_FAILURES_IN_A_ROW} emails in a row, so the problem is "
                               "probably not one address (see the messages above).")
                break
            if number < len(queue):
                sleep(PAUSE_BETWEEN_EMAILS)
    except KeyboardInterrupt:
        if in_flight:  # Brevo may have it: never send it again automatically
            try:
                log.append(round_id, in_flight[0], in_flight[1], "unknown", "stopped with Ctrl+C while sending")
            except SendLogError:
                pass  # the "pending" line already makes it count as unknown
            counts["unknown"] += 1
        stop_reason = "Stopped with Ctrl+C."
    return counts, stop_reason


# =============================================================================
# COMMANDS
# =============================================================================

def cmd_status(paths):
    """Print how far the current round has got. Reads local files only - sends nothing."""
    try:
        log = SendLog(paths.send_log)
    except SendLogError as error:
        raise Stop(str(error)) from None
    batches = read_batches(paths.batches) if paths.batches.exists() else []
    used = log.accepted_since(clock() - timedelta(hours=24))
    print(f"\n  Emails in the last 24 hours: {used} of {DAILY_LIMIT}")
    round_id = log.current_round()
    if not round_id:
        print("  No round has been started yet." + ("" if batches else " (No batch files - run python split_excel.py first.)"))
        if batches:
            print("  Start one with:  python send_emails.py --new-round")
        print("\nStatus report only - nothing was sent.")
        return 0
    states = log.states(round_id)
    print(f"  Round {round_id} (started {log.round_started(round_id):%Y-%m-%d %H:%M})")
    print(f"\n  {'Batch':<10}{'People':>7}{'Sent':>6}{'Unknown':>9}{'Rejected':>10}{'Left':>6}")
    rows = everyone(batches, states)
    for name, _ in batches:
        mine = [s.get("state") for b, _, s in rows if b == name]
        tally = Counter(mine)
        left = sum(1 for s in mine if s not in DONE)
        print(f"  {name:<10}{len(mine):>7}{tally['sent']:>6}{tally['unknown']:>9}{tally['rejected']:>10}{left:>6}")
    unknown = [(p["email"], s.get("detail", "")) for _, p, s in rows if s.get("state") == "unknown"]
    failing = [(p["email"], s.get("detail", "")) for _, p, s in rows if s.get("state") == "failed"]
    if unknown:
        print(f"\n  [WARN] {len(unknown)} address(es) MAY have the email (the connection broke). They are checked "
              "against Brevo's event log on the next --send run:")
        for email, detail in unknown:
            print(f"         {email}  ({detail[:80]})")
    if failing:
        print(f"\n  [INFO] {len(failing)} address(es) were refused by Brevo and will be tried again:")
        for email, detail in failing:
            print(f"         {email}  ({detail[:80]})")
    left = sum(1 for _, _, s in rows if s.get("state") not in DONE)
    print(f"\n  {left} people still to email in round {round_id}." if left else
          f"\n  Round {round_id} is COMPLETE. Start the next issue with:  python send_emails.py --new-round")
    print("\nStatus report only - nothing was sent.")
    return 0


def cmd_new_round(paths):
    """Start a new round: everybody in batches/ becomes due for ONE email. Sends nothing."""
    with SendLock(paths.lock):
        log = open_log(paths)
        today = clock().date()
        batches = read_batches(paths.batches) if paths.batches.exists() else []
        if not batches:
            raise Stop("No batch files in batches/. Run  python split_excel.py  first.")
        print("\nChecking Brevo (read-only)")
        settings = get_settings(paths)
        client = make_client(settings["api_key"])
        check_history(client, log, today)
        total = len(everyone(batches, {}))
        current = log.current_round()
        if current:
            left = [p["email"] for _, p, s in everyone(batches, log.states(current)) if s.get("state") not in DONE]
            if left:
                print(f"\n  [WARN] Round {current} is not finished: {len(left)} people have not had its email. "
                      "If you start a new round now they will only get the new one.")
        new_id = log.new_round_id(today)
        print(f"\n  New round {new_id}: {total} subscribers in {len(batches)} batch files will each get one email,")
        print(f"  at most {DAILY_LIMIT} in any 24 hours (python send_emails.py --send, once a day).")
        if ask(f"\nTo start it, type the round name ({new_id}), or press Enter to cancel: ") != new_id:
            print("Cancelled - no round was started.")
            return 0
        try:
            log.append(new_id, "", "", "round-start", f"{total} subscribers in {len(batches)} batch files")
        except SendLogError as error:
            raise Stop(str(error)) from None
        print(f"Round {new_id} started. Nothing was sent. Send the first portion with:  python send_emails.py --send")
        return 0


def cmd_send(paths, max_count=None):
    # ---- Safety lock 1: the switch at the top of this file -------------------
    if not PRODUCTION_SENDING_ENABLED:
        raise Stop("Production sending is switched OFF.\n"
                   "When you are ready, open send_emails.py and change\n"
                   "    PRODUCTION_SENDING_ENABLED = False\n"
                   "to\n"
                   "    PRODUCTION_SENDING_ENABLED = True")

    # ---- Safety lock 3: one run at a time, log readable and writable ---------
    with SendLock(paths.lock):
        log = open_log(paths)
        round_id = log.current_round()
        if not round_id:
            raise Stop("No round has been started yet. Run:  python send_emails.py --new-round")
        today = clock().date()

        # ---- Safety lock 4: complete, real, current newsletter content -------
        print("\n1) Newsletter content from newsletter/newsletter_data.txt")
        data, problems = read_newsletter_data(paths.data_file)
        more_problems, content_warnings = check_newsletter_data(data, today)
        problems += more_problems
        if problems:
            raise Stop("Fix these in newsletter/newsletter_data.txt first:\n    " + "\n    ".join(problems))
        print("  [OK]   every value present and checked; READY_TO_SEND_ON is today")

        # ---- Safety locks 5 + 6: the live template and Brevo's records --------
        print("\n2) Checking Brevo (read-only)")
        settings = get_settings(paths)
        client = make_client(settings["api_key"])
        credits = daily_credits_left(brevo(client.account))
        verify_template(client, settings, data, paths)
        reconcile_with_brevo(client, log, round_id, today)
        check_history(client, log, today)
        give_up_on_failing_addresses(log, round_id)

        # ---- Safety lock 7: today's allowance ----------------------------------
        window = sorted(t for t in log.possibly_accepted() if t >= clock() - timedelta(hours=24))
        used = len(window)
        allowance = DAILY_LIMIT - used
        explanation = f"{DAILY_LIMIT} in any 24 hours - {used} in the last 24 hours = {allowance}"
        if credits is not None:
            explanation += f"; Brevo credits left today: {credits}"
            allowance = min(allowance, credits)
        print(f"\n3) Today's allowance: {max(allowance, 0)}  ({explanation})")
        if window and used > 0:
            print(f"         The {used} email(s) of the last 24 hours stop counting between "
                  f"{window[0] + timedelta(hours=24, seconds=1):%Y-%m-%d %H:%M} and "
                  f"{window[-1] + timedelta(hours=24, seconds=1):%Y-%m-%d %H:%M}.")
        if allowance <= 0:
            if used >= DAILY_LIMIT:
                raise Stop(f"No emails may be sent right now - {used} went out in the last 24 hours (limit "
                           f"{DAILY_LIMIT}). More can be sent after "
                           f"{window[used - DAILY_LIMIT] + timedelta(hours=24, seconds=1):%Y-%m-%d %H:%M}.")
            raise Stop("No emails may be sent right now - Brevo says the account has no credits left today "
                       "(test emails count too). Try again tomorrow.")

        batches = read_batches(paths.batches) if paths.batches.exists() else []
        if not batches:
            raise Stop("No batch files in batches/. Run  python split_excel.py  first.")
        due = [(batch, person) for batch, person, state in everyone(batches, log.states(round_id))
               if state.get("state") not in DONE]
        if not due:
            print(f"\nEverybody in round {round_id} has had the email. Start the next issue with:"
                  "  python send_emails.py --new-round")
            return 0
        queue = due[:allowance if max_count is None else min(allowance, max_count)]

        shown_params = build_params(data, "(each subscriber's Name)", today)
        print("\nReady to send:")
        print(f"  Round      : {round_id}  ({len(due)} people still to email; {len(queue)} now"
              + (f", at most {max_count} by --max" if max_count is not None else "") + ")")
        print(f"  Batches    : {', '.join(sorted({b for b, _ in queue}))}")
        print(f"  Template   : {settings['template_id']}")
        print(f"  From       : {settings['sender_name']} <{settings['sender_email']}>")
        print(f"  Subject    : {email_subject(data)}")
        print(f"  First few  : {', '.join(p['email'] for _, p in queue[:3])}")
        print("\n  Brevo params in every email (from newsletter/newsletter_data.txt):")
        for line in params_table(shown_params, data):
            print("    " + line)
        print("\n  Numbered sections - every email shows exactly these entries, in this order:")
        for line in entries_report(data):
            print("    " + line)
        for warning in content_warnings:
            print(f"  [WARN] {warning}")

        # ---- Safety lock 8: type the number ------------------------------------
        print("\nThis emails REAL SUBSCRIBERS.")
        if ask(f"To confirm, type the number of emails ({len(queue)}), or press Enter to cancel: ") != str(len(queue)):
            print("Cancelled - nothing was sent.")
            return 0

        backup_log(paths.send_log, paths.log_backups, "before")
        print(f"\nSending {len(queue)} emails of round {round_id} ...")
        counts, stop_reason = send_queue(client, settings, data, log, round_id, queue, today)
        backup_log(paths.send_log, paths.log_backups, "after")

        left = sum(1 for _, _, s in everyone(batches, log.states(round_id)) if s.get("state") not in DONE)
        print("\n" + "=" * 66)
        print(f" Round {round_id}: {counts['sent']} sent, {counts['failed']} refused, {counts['rejected']} rejected, "
              f"{counts['unknown']} unknown (this run)")
        print("=" * 66)
        if stop_reason:
            print(stop_reason)
        print(f"{left} people still to email in this round." if left else
              f"Round {round_id} is COMPLETE. Start the next issue with:  python send_emails.py --new-round")
        print("Details: logs/production_send_log.csv")
        return 1 if stop_reason else 0


def cmd_dry_run(paths, max_count=None):
    """What --send would do right now, READ-ONLY: the same checks, then who would get the email, saved in
    logs/ with one example email. It sends nothing, writes nothing to the send log and works while
    PRODUCTION_SENDING_ENABLED is False. Every problem is listed, where a real --send stops at the first."""
    problems = []
    try:
        log = SendLog(paths.send_log)
    except SendLogError as error:
        raise Stop(str(error)) from None
    today = clock().date()
    round_id = log.current_round()

    print("\n1) Newsletter content from newsletter/newsletter_data.txt")
    data, read_problems = read_newsletter_data(paths.data_file)
    content_problems, content_warnings = check_newsletter_data(data, today)
    problems += read_problems + content_problems
    for problem in problems:
        print(f"  [FAIL] {problem}")
    if not problems:
        print("  [OK]   every value present and checked; READY_TO_SEND_ON is today")

    print("\n2) Checking Brevo (read-only)")
    settings = get_settings(paths)
    client = make_client(settings["api_key"])
    credits = daily_credits_left(brevo(client.account))
    for check in (lambda: verify_template(client, settings, data, paths), lambda: check_history(client, log, today)):
        try:
            check()
        except Stop as error:
            problems.append(str(error))
            print(f"  [FAIL] {error}")
    accepted = set()
    if round_id:  # --send first adds these to the log; here they are only read
        started = log.round_started(round_id).date()
        accepted = set(brevo(client.accepted_emails, round_tag(round_id), started - timedelta(days=1), today))

    used = sum(1 for t in log.possibly_accepted() if t >= clock() - timedelta(hours=24))
    allowance = DAILY_LIMIT - used
    explanation = f"{DAILY_LIMIT} in any 24 hours - {used} in the last 24 hours = {allowance}"
    if credits is not None:
        explanation += f"; Brevo credits left today: {credits}"
        allowance = min(allowance, credits)
    allowance = max(allowance, 0)
    print(f"\n3) Today's allowance: {allowance}  ({explanation})")
    if not allowance:
        problems.append("no emails may be sent right now - the 24-hour limit or Brevo's credits for today are used up")

    batches = read_batches(paths.batches) if paths.batches.exists() else []
    if not batches:
        raise Stop("No batch files in batches/. Run  python split_excel.py  first.")
    states = log.states(round_id) if round_id else {}
    due = [(batch, person) for batch, person, state in everyone(batches, states)
           if state.get("state") not in DONE and address_key(person["email"]) not in accepted]
    queue = due[:allowance if max_count is None else min(allowance, max_count)]
    command = "python send_emails.py --send" + (f" --max {max_count}" if max_count is not None else "")
    shown_round = round_id or log.new_round_id(today)

    print(f"\n4) What  {command}  would do now")
    if not round_id:
        print(f"  [INFO] No round has been started yet - --new-round would start round {shown_round} first.")
    print(f"  Round      : {shown_round}  ({len(due)} people still to email; {len(queue)} in this run)")
    print(f"  Batches    : {', '.join(sorted({b for b, _ in queue})) or '-'}")
    print(f"  Template   : {settings['template_id']}")
    print(f"  From       : {settings['sender_name']} <{settings['sender_email']}>")
    print(f"  Subject    : {email_subject(data)}")
    if queue:
        print(f"  First, last: {queue[0][1]['email']}  ...  {queue[-1][1]['email']}")
    print("  Sections   : every email shows exactly these entries, in this order:")
    for line in entries_report(data):
        print(f"               {line}")
    for warning in content_warnings:
        print(f"  [WARN] {warning}")

    paths.dry_run_list.parent.mkdir(exist_ok=True)
    with open(paths.dry_run_list, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["#", "Batch", "Email", "Name", "Surname"])
        for number, (batch, person) in enumerate(queue, start=1):
            writer.writerow([number, batch, person["email"], person["name"], person["surname"]])
    print(f"  [OK]   logs/{paths.dry_run_list.name}: the {len(queue)} recipient(s) in sending order")
    if queue:
        batch, person = queue[0]
        example = build_email(person, settings, data, shown_round, batch, today)
        paths.dry_run_example.write_text(json.dumps({"endpoint": "POST https://api.brevo.com/v3/smtp/email (NOT called)",
                                                     "first_email": example}, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        html = paths.master_template.read_text(encoding="utf-8") if paths.master_template.exists() else ""
        paths.dry_run_preview.write_text(render(html, example["params"]), encoding="utf-8")
        print(f"  [OK]   logs/{paths.dry_run_example.name} and logs/{paths.dry_run_preview.name}: the first email")

    print("\n5) Result")
    if problems:
        print(f"  NOT READY - {len(problems)} problem(s) above must be fixed before --send can send.")
    else:
        print(f"  READY - --send would email these {len(queue)} subscriber(s) now.")
    print("\nDRY RUN - nothing was sent and the send log was not changed.")
    steps = [] if PRODUCTION_SENDING_ENABLED else ["switch sending on (PRODUCTION_SENDING_ENABLED = True)"]
    steps += [] if round_id else ["python send_emails.py --new-round"]
    print("To send for real:  " + "  ->  ".join(steps + [command]))
    return 0 if not problems else 1


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # never crash on unusual characters in names

    parser = argparse.ArgumentParser(
        description="PRODUCTION sender. Without --send or --new-round it only shows a status report.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--send", action="store_true", help="send today's portion (only if every safety lock passes)")
    group.add_argument("--new-round", action="store_true", help="start a new round (sends nothing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --send: every check, then who would get the email - read-only, sends nothing")
    parser.add_argument("--max", type=int, metavar="N", help="with --send: send at most N emails in this run")
    args = parser.parse_args(argv)
    if (args.dry_run or args.max is not None) and not args.send:
        parser.error("--dry-run and --max only work together with --send")
    if args.max is not None and args.max < 1:
        parser.error("--max must be 1 or more")

    print("=" * 66)
    print(" Coin Edition newsletter - PRODUCTION" + ("   (DRY RUN - read-only, nothing is sent)" if args.dry_run else ""))
    print(" Sending is " + ("ENABLED" if PRODUCTION_SENDING_ENABLED else "DISABLED (safety switch is off)"))
    print("=" * 66)

    paths = Paths(PROJECT_DIR)
    try:
        if args.send and args.dry_run:
            return cmd_dry_run(paths, args.max)
        if args.send:
            return cmd_send(paths, args.max)
        if args.new_round:
            return cmd_new_round(paths)
        return cmd_status(paths)
    except (Stop, LockedError) as error:
        print(f"\n[STOPPED] {error}")
        print("Nothing was sent.")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped with Ctrl+C before sending - nothing was sent.")
        sys.exit(1)
