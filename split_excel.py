"""
split_excel.py - Clean the subscriber list and split it into batches of 300.

Reads : the subscriber file in data/ - a .csv (UTF-8 or Excel's "CSV", separated by commas,
        semicolons or tabs) or an .xlsx (first sheet). It needs an Email column; Name and
        Surname are optional (see recipients.COLUMN_NAMES for the header names accepted).
        If data/ holds more than one file, choose one with --input.
Writes: batches/batch_01.csv, batch_02.csv, ...  (at most 300 subscribers each)
        logs/removed_subscribers.csv             (every row that was left out, and why)
        logs/invalid_addresses.csv               (invalid-looking addresses - KEPT, see below)

Email addresses are NEVER changed: each one goes into the batches exactly as written in the file.
  * rows with a blank email are left out - there is no address to send to
  * an address written twice is sent once: later copies are left out (compared ignoring capitals
    and spaces, so nobody gets the email twice)
  * invalid-looking addresses are KEPT exactly as written: send_emails.py tries them like any other
    and logs Brevo's answer. They are listed in logs/invalid_addresses.csv.

Safety: this script sends to EVERYONE in the file. If the file has a column that looks like a
subscription status (Status, Subscribed, Opt-in, Unsubscribed, Bounced ...), it stops so that
unsubscribed people can't be emailed by mistake. Remove those people (and the column) first, or
add --ignore-status-column if every single person in the file agreed to receive the newsletter.

This script NEVER sends emails. It only reads the subscriber file and writes CSV files.

Usage:
    python split_excel.py                                  create the batch files
    python split_excel.py --input "data/subscribers.csv"   use this file
    python split_excel.py --overwrite                      replace batch files that already exist
    python split_excel.py --first-batch-size 280           batch_01 gets 280, every later batch 300
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from recipients import clean_email, find_column, is_valid_email, read_table, status_columns
from send_state import DONE, SendLog, SendLogError


# =============================================================================
# SETTINGS
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent

DATA_DIR = PROJECT_DIR / "data"
BATCHES_DIR = PROJECT_DIR / "batches"
REMOVED_REPORT = PROJECT_DIR / "logs" / "removed_subscribers.csv"
SEND_LOG_FILE = PROJECT_DIR / "logs" / "production_send_log.csv"  # written by send_emails.py

BATCH_SIZE = 300
SUBSCRIBER_FILE_TYPES = (".csv", ".xlsx")


# =============================================================================
# HELPERS
# =============================================================================

def stop(message):
    """Print an error message and end the script."""
    print(f"\n[STOPPED] {message}")
    print("No batch files were changed.")
    sys.exit(1)


def find_input_file(chosen):
    """The subscriber file: --input if given, otherwise the only .csv/.xlsx file in data/."""
    if chosen:
        path = Path(chosen)
        if not path.is_absolute():
            path = (PROJECT_DIR / path) if not path.exists() else path.resolve()
        if not path.exists():
            stop(f"Subscriber file not found: {path}")
        return path
    files = sorted(p for p in DATA_DIR.glob("*") if p.suffix.lower() in SUBSCRIBER_FILE_TYPES
                   and not p.name.startswith("~$"))  # ~$name.xlsx = Excel's temporary lock file
    if not files:
        stop(f"No subscriber file found in {DATA_DIR}\nPut your .csv or .xlsx file there and run again.")
    if len(files) > 1:
        stop("data/ holds more than one subscriber file:\n  " + "\n  ".join(f.name for f in files)
             + '\nChoose one, e.g.:  python split_excel.py --input "data/' + files[0].name + '"')
    return files[0]


# =============================================================================
# STEPS
# =============================================================================

def clean_subscribers(rows, columns):
    """Go through every row and keep every address exactly as written, each one once.

    Returns (kept, removed, empty_rows, invalid):
      kept       - list of {"Email", "Name", "Surname"} to put in the batches (Email unchanged)
      removed    - rows left out (blank email, or an address already listed), each with the reason
      empty_rows - number of completely empty rows that were skipped
      invalid    - rows whose address looks invalid: KEPT in the batches, listed here for the report
    """
    kept, removed, empty_rows, invalid = [], [], 0, []
    first_seen_on_row = {}  # tidied address -> file row where it first appeared

    for index, row in enumerate(rows):
        file_row = index + 2  # +2 because row 1 is the header
        email = str(row.get(columns["email"]) or "")  # exactly as written - never changed
        name = str(row.get(columns["name"]) or "").strip() if columns["name"] else ""
        surname = str(row.get(columns["surname"]) or "").strip() if columns["surname"] else ""
        key = clean_email(email)  # only to recognise the same address written twice

        if not (email.strip() or name or surname):
            empty_rows += 1
            continue

        if not email.strip():
            reason = "blank email"
        elif key in first_seen_on_row:
            reason = f"duplicate (same as row {first_seen_on_row[key]})"
        else:
            reason = None

        if reason:
            removed.append({"Row": file_row, "Email": email, "Name": name, "Surname": surname, "Reason": reason})
            continue
        first_seen_on_row[key] = file_row
        kept.append({"Email": email, "Name": name, "Surname": surname})
        if not is_valid_email(key):
            invalid.append({"Row": file_row, "Email": email, "Name": name, "Surname": surname,
                            "Reason": "looks invalid - kept exactly as written; Brevo's answer will be logged"})
    return kept, removed, empty_rows, invalid


def write_csv(path, rows, columns):
    # utf-8-sig makes Excel show accented names correctly when you open the CSV.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_batches(subscribers, first_size=BATCH_SIZE):
    """Save the subscribers as batches/batch_01.csv, batch_02.csv, ... - first_size in the first
    one, BATCH_SIZE in every later one."""
    BATCHES_DIR.mkdir(exist_ok=True)
    written, start = [], 0
    while start < len(subscribers):
        number = len(written) + 1
        size = first_size if number == 1 else BATCH_SIZE
        path = BATCHES_DIR / f"batch_{number:02d}.csv"
        chunk = subscribers[start:start + size]
        write_csv(path, chunk, ["Email", "Name", "Surname"])
        written.append((path.name, len(chunk)))
        start += size
    return written


def round_in_progress():
    """(round name, people still to email) if send_emails.py is in the middle of a round, else None."""
    try:
        log = SendLog(SEND_LOG_FILE)
    except SendLogError:
        return ("(unknown - the send log can't be read)", None)
    current = log.current_round()
    if not current:
        return None
    done = sum(1 for state in log.states(current).values() if state["state"] in DONE)
    return (current, done)


# =============================================================================
# MAIN
# =============================================================================

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # never crash on unusual characters in names

    parser = argparse.ArgumentParser(
        description="Clean the subscriber file (.csv or .xlsx) and split it into CSV batches. Never sends emails.")
    parser.add_argument("--input", help="the subscriber file to use (default: the only .csv/.xlsx in data/)")
    parser.add_argument("--overwrite", action="store_true", help="replace batch files that already exist")
    parser.add_argument("--ignore-status-column", action="store_true",
                        help="go ahead although the file has a Status/Subscribed/Opt-in... column")
    parser.add_argument("--first-batch-size", type=int, default=BATCH_SIZE, metavar="N",
                        help=f"put N subscribers in batch_01 (1-{BATCH_SIZE}); every later batch gets {BATCH_SIZE}")
    args = parser.parse_args()
    if not 1 <= args.first_batch_size <= BATCH_SIZE:
        stop(f"--first-batch-size must be between 1 and {BATCH_SIZE}.")

    print("=" * 66)
    print(" Coin Edition newsletter - split subscribers into batches")
    print(" (this script never sends emails)")
    print("=" * 66)

    old_batches = sorted(BATCHES_DIR.glob("batch_*.csv"))
    if old_batches and not args.overwrite:
        stop(f"batches/ already has {len(old_batches)} batch files. To replace them, run:\n"
             "    python split_excel.py --overwrite")

    input_file = find_input_file(args.input)
    try:
        headers, rows, description = read_table(input_file)
    except PermissionError:
        stop(f"Windows would not let Python open {input_file.name}. Close it in Excel and run again.")
    except (OSError, ValueError) as error:
        stop(f"Could not read {input_file.name}: {error}")
    print(f"\nReading {input_file.name} ({description})")

    columns = {what: find_column(headers, what) for what in ("email", "name", "surname")}
    if not columns["email"]:
        stop(f"Could not find an Email column. Columns in the file: {headers}")
    for what in ("name", "surname"):
        if not columns[what]:
            print(f"[WARN] No '{what.title()}' column found - it will be left empty.")

    flagged = [h for h in status_columns(headers) if h not in columns.values()]
    if flagged and not args.ignore_status_column:
        lines = []
        for header in flagged:
            values = Counter(str(row.get(header, "")).strip() or "(empty)" for row in rows)
            lines.append(f"  '{header}': " + ", ".join(f"{value} x{count}" for value, count in values.most_common(6)))
        stop("The file has column(s) that look like a subscription status:\n" + "\n".join(lines) + "\n"
             "This script would email EVERYONE in the file, including people who unsubscribed or bounced.\n"
             "Remove the people who must not be emailed (and the column), then run again - or add\n"
             "--ignore-status-column if every person in the file agreed to receive the newsletter.")

    if old_batches:
        current = round_in_progress()
        if current:
            print(f"[WARN] Round {current[0]} is in progress. Re-splitting is safe - nobody gets this round's")
            print("       email twice and the 300-a-day limit still holds - and new subscribers in the file")
            print("       will be included in this round.\n")

    kept, removed, empty_rows, invalid = clean_subscribers(rows, columns)
    if not kept:
        stop("No email addresses were found, so no batch files were written.")

    for old_file in old_batches:
        old_file.unlink()
    batches = write_batches(kept, args.first_batch_size)
    REMOVED_REPORT.parent.mkdir(exist_ok=True)
    write_csv(REMOVED_REPORT, removed, ["Row", "Email", "Name", "Surname", "Reason"])
    invalid_report = REMOVED_REPORT.parent / "invalid_addresses.csv"
    write_csv(invalid_report, invalid, ["Row", "Email", "Name", "Surname", "Reason"])

    blank = sum(1 for row in removed if row["Reason"] == "blank email")
    duplicates = sum(1 for row in removed if row["Reason"].startswith("duplicate"))

    print(f"\nRows in the file            : {len(rows):,}")
    if empty_rows:
        print(f"  empty rows skipped        : {empty_rows:,}")
    print(f"  blank emails left out     : {blank:,}")
    print(f"  repeated emails left out  : {duplicates:,}")
    print(f"Subscribers kept            : {len(kept):,}  (every address exactly as written in the file)")
    if invalid:
        print(f"  of which look invalid     : {len(invalid):,}  - KEPT and tried; Brevo's answer is logged")
        for row in invalid:
            print(f"      row {row['Row']}: {row['Email']}")

    print(f"\nBatches written to {BATCHES_DIR}:")
    for file_name, count in batches:
        print(f"  {file_name:<16} {count:>4} subscribers")
    print(f"  {'Total':<16} {len(kept):>4,} subscribers in {len(batches)} batches")

    if removed:
        print(f"\nRows left out are listed in {REMOVED_REPORT}")
    if invalid:
        print(f"Invalid-looking addresses (kept) are listed in {invalid_report}")
    print("\nDone. No emails were sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
