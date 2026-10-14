"""
template_sync.py - Compare the local master copy template/brevo_template_1.html with the LIVE
Brevo template (BREVO_TEMPLATE_ID in .env). It never sends an email and never changes Brevo:
you paste the HTML into Brevo yourself.

    python template_sync.py status   compare the two and check the live template (read-only)
    python template_sync.py pull     copy the LIVE template into the local master copy
                                     (the old local file is saved in template/backups/ first)

After you paste template/brevo_template_1.html into Brevo and save it, run "status": it should
say IDENTICAL and 0 problems. If Brevo's editor re-formatted the code (DIFFERENT, but 0
problems), run "pull" so the local master copy matches what Brevo really holds.

send_test.py and send_emails.py refuse to send while the two copies differ.
"""

import argparse
import sys
from pathlib import Path

from brevo_api import BrevoClient, BrevoError, SettingsError, load_settings
from newsletter_params import build_params, read_newsletter_data
from send_state import now
from template_check import check_rendered, check_template, render

PROJECT_DIR = Path(__file__).resolve().parent
MASTER = PROJECT_DIR / "template" / "brevo_template_1.html"
BACKUP_DIR = PROJECT_DIR / "template" / "backups"


def stop(message):
    print(f"\n[STOPPED] {message}")
    sys.exit(1)


def problems_in(html):
    """Template checks with the params built from the current newsletter_data.txt."""
    data, _ = read_newsletter_data()
    params = build_params(data, "Subscriber")
    problems, warnings = check_template(html, params)
    try:
        problems += check_rendered(render(html, params))
    except Exception as error:
        problems.append(f"the template could not be rendered: {error}")
    return problems, warnings


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Compare Brevo template 1 with the local master copy (read-only).")
    parser.add_argument("action", choices=["status", "pull"])
    args = parser.parse_args()

    try:
        settings = load_settings(PROJECT_DIR)
    except SettingsError as error:
        stop(str(error))
    template_id = settings["template_id"]
    try:
        live = BrevoClient(settings["api_key"]).template(template_id)
    except BrevoError as error:
        stop(str(error))
    live_html = live.get("htmlContent", "") or ""
    master_html = MASTER.read_bytes().decode("utf-8") if MASTER.exists() else None
    same = master_html is not None and master_html.replace("\r\n", "\n") == live_html.replace("\r\n", "\n")

    print(f"Brevo template {template_id} \"{live.get('name')}\" - subject \"{live.get('subject')}\", "
          f"{'active' if live.get('isActive') else 'INACTIVE'}, last changed {live.get('modifiedAt')}")
    print(f"Local master copy {MASTER.relative_to(PROJECT_DIR)}: "
          + ("missing" if master_html is None else "IDENTICAL to the live template" if same
             else "DIFFERENT from the live template"))

    if args.action == "status":
        problems, warnings = problems_in(live_html)
        print(f"Checks of the LIVE template: {len(problems)} problem(s)")
        for line in problems[:40] + warnings:
            print(f"  - {line}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        return 0 if same and not problems else 1

    if same:
        print("Nothing to do - the two are already identical.")
        return 0
    if master_html is not None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"brevo_template_1_local_{now():%Y-%m-%d_%H%M%S}.html"
        backup.write_bytes(master_html.encode("utf-8"))
        print(f"Old local copy saved as {backup.relative_to(PROJECT_DIR)}")
    MASTER.parent.mkdir(exist_ok=True)
    MASTER.write_bytes(live_html.encode("utf-8"))
    print("The live template is now the local master copy. Nothing was changed in Brevo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
