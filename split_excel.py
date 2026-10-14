"""
split_excel.py - Clean the subscriber list and split it into batches of 300.

Reads : the subscriber file in data/ - a .csv (UTF-8 or Excel's "CSV", separated by commas,
        semicolons or tabs) or an .xlsx (first sheet). It needs an Email column; Name and
        Surname are optional (see recipients.COLUMN_NAMES for the header names accepted).
        If data/ holds more than one file, choose one with --input.
Writes: batches/batch_01.csv, batch_02.csv, ...  (at most 300 subscribers each)
        logs/removed_subscribers.csv             (every row that was removed, and why)

Cleaning:
  * spaces, hidden characters and "mailto:" are removed; emails are made lower-case
  * rows with a blank email are removed
  * rows with an invalid email address are removed
  * duplicate emails are removed (the first one is kept)

Safety: this script sends to EVERYONE in the file. If the file has a column that looks like a
subscription status (Status, Subscribed, Opt-in, Unsubscribed, Bounced ...), it stops so that
unsubscribed people can't be emailed by mistake. Remove those people (and the column) first, or
add --ignore-status-column if every single person in the file agreed to receive the newsletter.

This script NEVER sends emails. It only reads the subscriber file and writes CSV files.

Usage:
    python split_excel.py                                  create the batch files
    python split_excel.py --input "data/subscribers.csv"   use this file
    python split_excel.py --overwrite                      replace batch files that already exist
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
    """Go through every row and keep only valid, unique email addresses.

    Returns (kept, removed, empty_rows):
      kept       - list of {"Email", "Name", "Surname"} to put in the batches
      removed    - list of removed rows, each with the reason
      empty_rows - number of completely empty rows that were skipped
    """
    kept, removed, empty_rows = [], [], 0
    first_seen_on_row = {}  # email -> file row where it first appeared

    for index, row in enumerate(rows):
        file_row = index + 2  # +2 because row 1 is the header
        original_email = str(row.get(columns["email"]) or "").strip()
        name = str(row.get(columns["name"]) or "").strip() if columns["name"] else ""
        surname = str(row.get(columns["surname"]) or "").strip() if columns["surname"] else ""
        email = clean_email(original_email)

        if not (email or name or surname):
            empty_rows += 1
            continue

        if not email:
            reason = "blank email"
        elif not is_valid_email(email):
            reason = "invalid email"
        elif email in first_seen_on_row:
            reason = f"duplicate (same as row {first_seen_on_row[email]})"
        else:
            reason = None

        if reason:
            removed.append({"Row": file_row, "Email": original_email, "Name": name, "Surname": surname,
                            "Reason": reason})
        else:
            first_seen_on_row[email] = file_row
            kept.append({"Email": email, "Name": name, "Surname": surname})
    return kept, removed, empty_rows


def write_csv(path, rows, columns):
    # utf-8-sig makes Excel show accented names correctly when you open the CSV.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_batches(subscribers):
    """Save the subscribers as batches/batch_01.csv, batch_02.csv, ... (BATCH_SIZE each)."""
    BATCHES_DIR.mkdir(exist_ok=True)
    written = []
    for start in range(0, len(subscribers), BATCH_SIZE):
        number = start // BATCH_SIZE + 1
        path = BATCHES_DIR / f"batch_{number:02d}.csv"
        chunk = subscribers[start:start + BATCH_SIZE]
        write_csv(path, chunk, ["Email", "Name", "Surname"])
        written.append((path.name, len(chunk)))
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
    args = parser.parse_args()

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

    kept, removed, empty_rows = clean_subscribers(rows, columns)
    if not kept:
        stop("No valid email addresses were found, so no batch files were written.")

    for old_file in old_batches:
        old_file.unlink()
    batches = write_batches(kept)
    REMOVED_REPORT.parent.mkdir(exist_ok=True)
    write_csv(REMOVED_REPORT, removed, ["Row", "Email", "Name", "Surname", "Reason"])

    blank = sum(1 for row in removed if row["Reason"] == "blank email")
    invalid = sum(1 for row in removed if row["Reason"] == "invalid email")
    duplicates = sum(1 for row in removed if row["Reason"].startswith("duplicate"))

    print(f"\nRows in the file            : {len(rows):,}")
    if empty_rows:
        print(f"  empty rows skipped        : {empty_rows:,}")
    print(f"  blank emails removed      : {blank:,}")
    print(f"  invalid emails removed    : {invalid:,}")
    print(f"  duplicate emails removed  : {duplicates:,}")
    print(f"Subscribers kept            : {len(kept):,}")

    print(f"\nBatches written to {BATCHES_DIR}:")
    for file_name, count in batches:
        print(f"  {file_name:<16} {count:>4} subscribers")
    print(f"  {'Total':<16} {len(kept):>4,} subscribers in {len(batches)} batches")

    if removed:
        print(f"\nRemoved rows are listed in {REMOVED_REPORT}")
    print("\nDone. No emails were sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
