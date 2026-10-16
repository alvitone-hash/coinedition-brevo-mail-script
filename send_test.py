"""
send_test.py - Send the Coin Edition newsletter to the TEST address only.

This is the TEST script. It:
  1. reads your Brevo settings from .env (the API key is never printed)
  2. reads tests/test_recipients.csv - and ONLY that file - and stops unless
     it contains exactly 1 email address (REQUIRED_RECIPIENTS)
  3. reads the newsletter content from newsletter/newsletter_data.txt and builds
     the Brevo params with newsletter_params.py - the SAME code production uses
  4. checks with Brevo (read-only) that the API key works, that the template is
     active, and that every {{ params.x }} placeholder, link and the unsubscribe
     link are right - the SAME checks production makes (template_check.py)
  5. saves the exact API payloads and a local preview of the email in logs/
  6. only if every check passed: shows the test address and waits for you to type YES,
     then sends the template to it

A test email is exactly what subscribers get: the same subject (the first Top Stories heading,
STORY_1_TITLE), template and params as send_emails.py. Only --allow-sample-content labels it.

Run it from the project folder, with the virtual environment active:

    python send_test.py --dry-run     do all the checks, send nothing
    python send_test.py --dry-run --local-template
                                      the same, but check template/brevo_template_1.html
                                      (before you paste it into Brevo) instead of the live one
    python send_test.py               send the test (asks you to type YES first)

Testing before the content is confirmed (TEST ONLY):

    python send_test.py --dry-run --allow-sample-content
    python send_test.py --allow-sample-content
                                      READY_TO_SEND_ON not being today and values still marked as
                                      sample no longer block the TEST - they are listed as [SAMPLE].
                                      Every other check still applies (recipients, API key, template,
                                      live = master copy, params, links, sender, YES). Every email is
                                      labelled: the subject starts with [TEST – UNVERIFIED SAMPLE
                                      CONTENT], and so do the date line and the intro sentence.
                                      send_emails.py has no such option - production always refuses
                                      unverified content.

It never reads data/ or batches/. Real subscribers are only ever emailed by
send_emails.py, which is switched off by default.
"""

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

from brevo_api import BrevoClient, BrevoError, SettingsError, daily_credits_left, load_settings
from newsletter_params import (
    DATA_FILE, URL_PARAMS,
    build_params, check_newsletter_data, check_values, email_subject, entries, entries_report, params_table,
    read_newsletter_data, readiness_problems,
)
from recipients import clean_email, find_column, is_valid_email, read_table
from send_state import LockedError, SendLock, now
from template_check import check_rendered, check_template, check_url_params, link_report, render


# =============================================================================
# SETTINGS
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent

# The ONLY recipient file this script reads. It is hard-coded on purpose:
# there is no option to point it at another file, so the real subscriber
# list in data/ or batches/ can never be used by mistake.
TEST_RECIPIENTS_FILE = PROJECT_DIR / "tests" / "test_recipients.csv"
REQUIRED_RECIPIENTS = 1      # the file must hold exactly this many addresses, or nothing is sent

TEST_LOG_FILE = PROJECT_DIR / "logs" / "test_send_log.csv"        # every test send
PAYLOAD_FILE = PROJECT_DIR / "logs" / "dry_run_payload.json"      # what would be sent (no API key)
PREVIEW_FILE = PROJECT_DIR / "logs" / "dry_run_preview.html"      # local preview of the email
MASTER_TEMPLATE = PROJECT_DIR / "template" / "brevo_template_1.html"
LOCK_FILE = PROJECT_DIR / "logs" / "send.lock"                    # shared with send_emails.py

PAUSE_BETWEEN_EMAILS = 1     # seconds

# Added to every test email so you can filter for it in Brevo's logs.
BREVO_TAG = "coinedition-newsletter-test"

# Sent as {{ params.first_name }} when a recipient's Name is empty ("Hi there,").
DEFAULT_FIRST_NAME = "there"

# The example addresses that come in tests/test_recipients.csv. They belong to
# real strangers, so nothing is sent until you replace them with your own.
EXAMPLE_ADDRESSES = {"test1@gmail.com", "test2@gmail.com", "test3@gmail.com", "test4@gmail.com"}

# --allow-sample-content: how a test email with UNVERIFIED content is labelled. The labels go into
# the subject and two existing text values (the red date line and the intro sentence), so the
# design is not changed. Content that is confirmed for today is never labelled.
SAMPLE_SUBJECT_PREFIX = "[TEST – UNVERIFIED SAMPLE CONTENT]"
SAMPLE_DATE_LABEL = "TEST EMAIL – UNVERIFIED SAMPLE CONTENT"
SAMPLE_INTRO_NOTE = ("This is a TEST email and its content has NOT been verified – values marked "
                     "“sample” are placeholders.")


# =============================================================================
# SMALL HELPERS
# =============================================================================

def stop(message):
    """Print an error message and end the script before anything is sent."""
    print(f"\n[STOPPED] {message}")
    print("Nothing was sent.")
    sys.exit(1)


def ask(question):
    """Ask a question in the terminal. Returns "" if nobody can answer (e.g. a scheduled task)."""
    try:
        return input(question).strip()
    except EOFError:
        return ""


# =============================================================================
# STEP 2 - THE TEST RECIPIENT
# =============================================================================

def read_test_recipients():
    """Read tests/test_recipients.csv. Stops unless it holds exactly REQUIRED_RECIPIENTS addresses (none of them an
    example address). Every address is sent to exactly as written - never tidied or fixed; one that looks
    invalid is still tried, with a warning, and Brevo's answer is logged."""
    if not TEST_RECIPIENTS_FILE.exists():
        stop(f"Test recipients file not found: {TEST_RECIPIENTS_FILE}")
    headers, rows, _ = read_table(TEST_RECIPIENTS_FILE)
    email_column = find_column(headers, "email")
    if not email_column:
        stop(f"tests/test_recipients.csv needs an 'Email' column. Columns found: {headers}")
    name_column, surname_column = find_column(headers, "name"), find_column(headers, "surname")

    recipients, problems, warnings, seen = [], [], [], set()
    for line, row in enumerate(rows, start=2):
        email = str(row.get(email_column) or "")  # sent exactly as written - never tidied or fixed
        key = clean_email(email)                  # only for the checks below
        name = (row.get(name_column) or "").strip() if name_column else ""
        surname = (row.get(surname_column) or "").strip() if surname_column else ""
        if not (key or name or surname):
            continue  # skip empty lines
        if not key:
            problems.append(f"line {line}: the Email is empty")
        elif key in EXAMPLE_ADDRESSES:
            problems.append(f"line {line}: '{email}' is an example address - replace it with your own test address")
        elif key in seen:
            problems.append(f"line {line}: '{email}' is listed twice")
        elif not is_valid_email(key):
            warnings.append(f"line {line}: '{email}' does not look like a valid email address - it is still tried "
                            "exactly as written, and Brevo's answer is logged")
        seen.add(key)
        recipients.append({"email": email, "name": name, "surname": surname})
    for warning in warnings:
        print(f"  [WARN] {warning}")

    if len(recipients) != REQUIRED_RECIPIENTS:
        problems.append(f"the file has {len(recipients)} recipients - it must have exactly {REQUIRED_RECIPIENTS}")
    if problems:
        print("  Problems found in tests/test_recipients.csv:")
        for problem in problems:
            print(f"    - {problem}")
        stop("Fix tests/test_recipients.csv and run the script again.")
    return recipients


# =============================================================================
# STEP 4 - READ-ONLY CHECKS AGAINST YOUR BREVO ACCOUNT
# =============================================================================

def check_brevo(client, settings, params, local_template=False):
    """Check the API key, the template and the sender with Brevo. Nothing is sent here.

    local_template=True checks template/brevo_template_1.html instead of the live template
    (to try the corrected HTML before pasting it into Brevo).
    Returns (html that was checked, problems, warnings, credits left today, the template's subject).
    """
    problems, warnings = [], []
    account = client.account()
    print("  [OK]   API key accepted by Brevo")
    credits = daily_credits_left(account)
    for plan in account.get("plan", []):
        if plan.get("type") != "sms":
            print(f"  [INFO] Brevo plan: {plan.get('type')}, email credits left today: {plan.get('credits')}")

    template = client.template(settings["template_id"])
    live_html = template.get("htmlContent", "")
    print(f"  [OK]   Template {settings['template_id']} found: \"{template.get('name', '')}\"")
    print(f"         Template subject: {template.get('subject', '')} (not used - every email's subject is "
          "STORY_1_TITLE)")
    if template.get("isActive") is False:
        problems.append(f"Template {settings['template_id']} is INACTIVE in Brevo - switch it on in Brevo's template list")

    master_html = MASTER_TEMPLATE.read_text(encoding="utf-8") if MASTER_TEMPLATE.exists() else None
    if local_template:
        if master_html is None:
            stop("template/brevo_template_1.html is missing.")
        html = master_html
        print("  [INFO] Checking the LOCAL file template/brevo_template_1.html (--local-template), not the live one")
    else:
        html = live_html
    which = "local file" if local_template else "live template"
    template_problems, template_warnings = check_template(html, params)
    problems += [f"{which}: {p}" for p in template_problems]
    warnings += template_warnings
    if not template_problems:
        print(f"  [OK]   {which}: every placeholder matches the {len(params)} params this script sends "
              "(exact names), |safe only on the safe-HTML values, unsubscribe link present")
    if master_html is None:
        problems.append("template/brevo_template_1.html (local master copy) is missing - run: python template_sync.py pull")
    elif master_html.replace("\r\n", "\n") != live_html.replace("\r\n", "\n"):
        problems.append("the LIVE template in Brevo is not template/brevo_template_1.html yet - paste that file into "
                        "Brevo template 1 (check afterwards with: python template_sync.py status)")
    else:
        print("  [OK]   Live template is identical to the local master copy template/brevo_template_1.html")

    senders = {clean_email(s.get("email")): s for s in client.senders()}
    sender = senders.get(settings["sender_email"])
    if sender is None:
        problems.append(f"{settings['sender_email']} is not in your Brevo senders list - add it in Brevo: "
                        "Senders, Domains & Dedicated IPs > Senders")
    elif sender.get("active") is False:
        problems.append(f"{settings['sender_email']} is in Brevo but not verified yet")
    else:
        print(f"  [OK]   Sender {settings['sender_email']} is set up in Brevo")
    return html, problems, warnings, credits, template.get("subject", "")


# =============================================================================
# STEP 6 - SENDING
# =============================================================================

def build_email(recipient, settings, data, today):
    """The JSON body for ONE email, in the format Brevo's /v3/smtp/email expects."""
    first_name = recipient["name"] or DEFAULT_FIRST_NAME
    full_name = " ".join(part for part in (recipient["name"], recipient["surname"]) if part)
    to = {"email": recipient["email"]}
    if full_name:
        to["name"] = full_name
    return {
        "sender": {"name": settings["sender_name"], "email": settings["sender_email"]},
        "to": [to],  # one recipient per email, so nobody sees the other addresses
        "templateId": settings["template_id"],
        "subject": email_subject(data),  # the production subject; Brevo uses it instead of the template's
        "params": build_params(data, first_name, today),  # built from newsletter_data.txt, same as production
        "tags": [BREVO_TAG],
    }


def label_as_sample(payload, subject):
    """The same email, labelled as a TEST with UNVERIFIED content (--allow-sample-content only).

    The subject is the newsletter's subject (STORY_1_TITLE) with SAMPLE_SUBJECT_PREFIX in front, and
    the date line and the intro sentence start with the labels. Every other param stays exactly as
    built from the data file.
    """
    params = dict(payload["params"])
    params["date"] = f"{SAMPLE_DATE_LABEL} · {params['date']}"
    params["intro_description"] = f"{SAMPLE_INTRO_NOTE} {params['intro_description']}"
    return dict(payload, params=params, subject=f"{SAMPLE_SUBJECT_PREFIX} {subject}".strip())


def log_result(recipient, status, detail):
    """Add one line to logs/test_send_log.csv, so you have a record of every test send."""
    TEST_LOG_FILE.parent.mkdir(exist_ok=True)
    is_new_file = not TEST_LOG_FILE.exists()
    with open(TEST_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["time", "email", "result", "message_id_or_error"])
        writer.writerow([now().strftime("%Y-%m-%d %H:%M:%S"), recipient["email"], status, detail])


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # never crash on unusual characters in names

    parser = argparse.ArgumentParser(
        description="Send the newsletter to the TEST address in tests/test_recipients.csv only.")
    parser.add_argument("--dry-run", action="store_true", help="do all the checks, but send nothing")
    parser.add_argument("--local-template", action="store_true",
                        help="with --dry-run: check template/brevo_template_1.html instead of the live template")
    parser.add_argument("--allow-sample-content", action="store_true",
                        help="TEST ONLY: go ahead although READY_TO_SEND_ON is not today or values are still "
                             "marked as sample; every other check still applies and every email is labelled "
                             "TEST / UNVERIFIED SAMPLE CONTENT (production has no such option)")
    args = parser.parse_args(argv)
    if args.local_template and not args.dry_run:
        parser.error("--local-template only works together with --dry-run (Brevo always sends the live template)")
    today = date.today()

    print("=" * 66)
    print(" Coin Edition newsletter - TEST SEND" + ("   (DRY RUN - nothing will be sent)" if args.dry_run else ""))
    if args.allow_sample_content:
        print(" --allow-sample-content: unverified content allowed for this TEST only - labelled in every email")
    print("=" * 66)

    print("\n1) Settings from .env")
    try:
        settings = load_settings(PROJECT_DIR)
    except SettingsError as error:
        stop(str(error))
    print("  [OK]   API key loaded (hidden)")
    print(f"  [OK]   Template ID {settings['template_id']}, sender {settings['sender_name']} <{settings['sender_email']}>")

    print("\n2) Test recipients from tests/test_recipients.csv")
    recipients = read_test_recipients()
    for recipient in recipients:
        print(f"  [OK]   {recipient['email']:<36} first_name = {recipient['name'] or DEFAULT_FIRST_NAME}")

    print("\n3) Newsletter content from newsletter/newsletter_data.txt")
    data, problems = read_newsletter_data(DATA_FILE)
    if args.allow_sample_content:
        # TEST ONLY: READY_TO_SEND_ON and sample values are listed as [SAMPLE] instead of blocking.
        # Every other content check (check_values) still blocks; the emails are labelled in step 5.
        more_problems, warnings = check_values(data)
        unverified = readiness_problems(data, today)
    else:
        more_problems, warnings = check_newsletter_data(data, today)  # exactly the production check
        unverified = []
    problems += more_problems
    sample_mode = bool(unverified)
    filled = sum(1 for value in data.values() if value)
    print(f"  [INFO] {len(data)} values read ({filled} filled in, {len(data) - filled} left empty)")
    for problem in problems:
        print(f"  [FAIL] {problem}")
    for problem in unverified:
        print(f"  [SAMPLE] {problem}")
    if not problems:
        print("  [OK]   every required value present; URLs, colours, bold markup checked"
              + ("" if sample_mode else "; no sample content; READY_TO_SEND_ON is today"))
    if sample_mode:
        print(f"  [INFO] --allow-sample-content: the {len(unverified)} [SAMPLE] item(s) above do not block this TEST, "
              "but every test email is labelled as UNVERIFIED SAMPLE CONTENT. send_emails.py refuses such content.")
    elif args.allow_sample_content:
        print("  [INFO] The content is confirmed for today, so the emails are NOT labelled "
              "(--allow-sample-content changes nothing)")
    elif readiness_problems(data, today):
        print("  [INFO] To test anyway, with every email labelled as unverified sample content:\n"
              "             python send_test.py --dry-run --allow-sample-content")
    print("  [INFO] Numbered sections - the email shows exactly these entries, in this order:")
    for line in entries_report(data):
        print(f"           {line}")
    for warning in warnings:
        print(f"  [WARN] {warning}")
    shown_params = build_params(data, "(each recipient's Name)", today)
    url_problems = check_url_params(shown_params)
    problems += url_problems
    urls = [param for param in URL_PARAMS if shown_params[param]]
    articles = sum(1 for story in entries(data, "STORY") if story["URL"])
    print(f"  [INFO] {len(urls)} web addresses in the params, plus {articles} story article URL(s) in the "
          "Top Stories rows (not shown as links)")

    print("\n4) Checking your Brevo account (read-only - nothing is sent)")
    client = BrevoClient(settings["api_key"])
    try:
        html, brevo_problems, brevo_warnings, credits, _ = check_brevo(client, settings, shown_params,
                                                                       local_template=args.local_template)
    except BrevoError as error:
        stop(str(error))
    problems += brevo_problems
    for problem in brevo_problems:
        print(f"  [FAIL] {problem}")
    for warning in brevo_warnings:
        print(f"  [WARN] {warning}")

    print("\n5) The exact email payloads and a local preview (nothing is sent)")
    payloads = [build_email(r, settings, data, today) for r in recipients]
    if sample_mode:
        payloads = [label_as_sample(payload, email_subject(data)) for payload in payloads]
    PAYLOAD_FILE.parent.mkdir(exist_ok=True)
    PAYLOAD_FILE.write_text(json.dumps({"endpoint": "POST https://api.brevo.com/v3/smtp/email (NOT called)",
                                        "payloads": payloads}, indent=2, ensure_ascii=False), encoding="utf-8")
    preview = render(html, payloads[0]["params"])
    PREVIEW_FILE.write_text(preview, encoding="utf-8")
    render_problems = check_rendered(preview)
    problems += render_problems
    for problem in render_problems:
        print(f"  [FAIL] preview: {problem}")
    links, images = link_report(preview)
    print(f"  [OK]   {PAYLOAD_FILE.relative_to(PROJECT_DIR)}: {len(payloads)} payloads, "
          f"{len(payloads[0]['params'])} params each (no API key in it)")
    print(f"  [INFO] Subject of every test email: {payloads[0]['subject']}")
    summary = "all https" if not render_problems else f"{len(render_problems)} problem(s) listed above"
    print(f"  [{'OK' if not render_problems else 'FAIL'}]   {PREVIEW_FILE.relative_to(PROJECT_DIR)}: "
          f"{len(links)} links and {len(images)} images, {summary} (open it in a browser to look at it)")
    print("         The preview is a local approximation - only a real test email shows what an inbox displays.")

    print(f"\n  Brevo params in every email ({len(shown_params)} - only first_name differs per recipient):")
    for line in params_table(shown_params, data):
        print("    " + line)
    if sample_mode:
        labelled = payloads[0]
        print("\n  Labels in every test email (the content is NOT verified - see [SAMPLE] in step 3):")
        print(f"    subject                 {labelled['subject']}")
        print(f"    date                    {labelled['params']['date']}")
        print(f"    intro_description       {labelled['params']['intro_description'][:70]}...")

    print("\n6) Result")
    if problems:
        print(f"  NOT READY - {len(problems)} problem(s) above must be fixed before a test email can be sent.")
    elif sample_mode:
        print(f"  READY FOR A LABELLED TEST - every check passed; {len(unverified)} unverified item(s) allowed by "
              "--allow-sample-content (production would refuse them).")
    else:
        print("  READY - every check passed.")
    command = "python send_test.py" + (" --allow-sample-content" if sample_mode else "")
    if args.dry_run:
        print("\nDry run finished - nothing was sent.")
        if not problems:
            print(f"To send the test for real, run:  {command}")
        return 0 if not problems else 1
    if problems:
        stop(f"{len(problems)} problem(s) must be fixed first (run with --dry-run to see them all).")

    print(f"\n  Template : {settings['template_id']}")
    print(f"  From     : {settings['sender_name']} <{settings['sender_email']}>")
    print(f"  Subject  : {payloads[0]['subject']}")
    if sample_mode:
        print(f"  Content  : NOT VERIFIED - {len(unverified)} [SAMPLE] item(s), labelled in every email")
    print(f"  To       : {len(recipients)} test addresses, one separate email each:")
    for recipient in recipients:
        print(f"               {recipient['email']}")
    what = "test emails with UNVERIFIED sample content" if sample_mode else "test emails"
    answer = ask(f"\nType YES to send {len(recipients)} {what} (anything else cancels): ")
    if answer.upper() != "YES":
        print("Cancelled - nothing was sent.")
        return 0

    sent = failed = 0
    try:
        with SendLock(LOCK_FILE):
            if credits is not None and credits < len(recipients):
                stop(f"Brevo says only {credits} email(s) can be sent today - not enough for {len(recipients)}.")
            print("\n7) Sending")
            # exactly the payloads checked, saved and previewed above
            for number, (recipient, payload) in enumerate(zip(recipients, payloads), start=1):
                result = client.send_email(payload)
                log_result(recipient, result.status, result.detail)
                if result.status == "sent":
                    sent += 1
                    print(f"  [{number}/{len(recipients)}] SENT    {recipient['email']}   message id: {result.detail}")
                else:
                    failed += 1
                    print(f"  [{number}/{len(recipients)}] {result.status.upper():<7} {recipient['email']}")
                    print(f"           {result.detail}")
                    if result.status == "unknown":
                        print("           It MAY have been sent - check Brevo > Transactional > Logs before trying again.")
                if result.stop:
                    print(f"  Stopped: {result.stop}")
                    break
                if number < len(recipients):
                    time.sleep(PAUSE_BETWEEN_EMAILS)
    except LockedError as error:
        stop(str(error))

    print("\n" + "=" * 66)
    print(f" Test finished: {sent} accepted by Brevo, {failed} not")
    print("=" * 66)
    if sent:
        print("Brevo accepted the 'SENT' emails (it gave each a message id). They usually arrive within")
        print("a minute or two - check each inbox, including the Spam and Promotions tabs.")
        print(f"Brevo's record: Transactional > Logs (tag: {BREVO_TAG}).")
    print("Results saved to logs/test_send_log.csv")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped with Ctrl+C. Any email marked SENT above was sent; the rest were not.")
        sys.exit(1)
