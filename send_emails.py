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
     (send_test.py --allow-sample-content is for the 4 test addresses only).
  5. The live Brevo template must pass every check and match template/brevo_template_1.html.
  6. Brevo's records must agree with the local send log.
  7. Today's allowance (see above) must be more than 0.
  8. You must type the number of emails to confirm.

Usage
-----
    python send_emails.py               status report only - sends nothing, no internet needed
    python send_emails.py --new-round   start a new round (sends nothing)
    python send_emails.py --send        send today's portion (only if every safety lock passes)
"""

import argparse
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from brevo_api import BrevoClient, BrevoError, SettingsError, daily_credits_left, load_settings
from newsletter_params import build_params, check_newsletter_data, params_table, read_newsletter_data
from recipients import clean_email, find_column, is_valid_email, read_table
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


class Stop(Exception):
    """Ends the run BEFORE any email of this run is sent, with a message for the user."""


# =============================================================================
# READING THE BATCHES AND THE LOG
# =============================================================================

def read_batches(batches_dir):
    """[(batch name, [person, ...]), ...] in number order. Invalid addresses are skipped."""
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
            email = clean_email(row.get(email_column))
            if is_valid_email(email):
                people.append({"email": email,
                               "name": (row.get(name_column) or "").strip() if name_column else "",
                               "surname": (row.get(surname_column) or "").strip() if surname_column else ""})
        batches.append((path.stem, people))
    return batches


def everyone(batches, states):
    """[(batch, person, state)] for everybody, in batch order. An address that appears in two
    batches (after a re-split) is listed once."""
    seen, result = set(), []
    for batch, people in batches:
        for person in people:
            if person["email"] not in seen:
                seen.add(person["email"])
                result.append((batch, person, states.get(person["email"], {})))
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


def cmd_send(paths):
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
        queue = due[:allowance]

        shown_params = build_params(data, "(each subscriber's Name)", today)
        print("\nReady to send:")
        print(f"  Round      : {round_id}  ({len(due)} people still to email; {len(queue)} now)")
        print(f"  Batches    : {', '.join(sorted({b for b, _ in queue}))}")
        print(f"  Template   : {settings['template_id']}")
        print(f"  From       : {settings['sender_name']} <{settings['sender_email']}>")
        print(f"  First few  : {', '.join(p['email'] for _, p in queue[:3])}")
        print("\n  Brevo params in every email (from newsletter/newsletter_data.txt):")
        for line in params_table(shown_params, data):
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
    args = parser.parse_args(argv)

    print("=" * 66)
    print(" Coin Edition newsletter - PRODUCTION")
    print(" Sending is " + ("ENABLED" if PRODUCTION_SENDING_ENABLED else "DISABLED (safety switch is off)"))
    print("=" * 66)

    paths = Paths(PROJECT_DIR)
    try:
        if args.send:
            return cmd_send(paths)
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
