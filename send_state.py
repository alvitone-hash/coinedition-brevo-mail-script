"""
send_state.py - The production send log: who has had which round's email, and how many
emails went out in the last 24 hours. Used by send_emails.py and split_excel.py.

logs/production_send_log.csv is APPEND-ONLY - never edit, move or delete it.
Columns: time, round, batch, email, status, detail

  status       meaning
  round-start  a new round begins (one pass through the subscriber list = one email each)
  pending      written to disk BEFORE the email is handed to Brevo
  sent         Brevo accepted it (detail = Brevo's message id)
  failed       Brevo refused it, so it was NOT sent - tried again on a later run
  rejected     Brevo refused this ADDRESS (or it kept failing) - not tried again this round
  unknown      the connection broke mid-send - it MAY have been sent - never re-sent automatically

A "pending" line with no result after it means the script stopped (crash, closed window,
power cut, log file locked) while that email was being handed to Brevo. It counts as
"unknown", so a crash can never cause a second email to the same person. Every line is
flushed AND synced to disk before the script carries on.

The log is not the only safeguard: before every run send_emails.py also checks Brevo's own
records (credits left today, addresses already accepted in this round), because a local
file can be deleted or replaced by an older copy.
"""

import csv
import io
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

LOG_COLUMNS = ["time", "round", "batch", "email", "status", "detail"]
RESULTS = {"sent", "failed", "rejected", "unknown"}
STATUSES = RESULTS | {"pending", "round-start"}
DONE = {"sent", "unknown", "rejected"}  # nobody in one of these states is emailed again this round
GIVE_UP_AFTER = 3                       # refusals of one address, while other emails went through
OTHERS_WENT_THROUGH_WITHIN = timedelta(hours=1)
ROUND_TAG_PREFIX = "coinedition-round-"


class SendLogError(Exception):
    """The send log can't be read or written (the message says why and what to do)."""


class LockedError(Exception):
    """Another sending run is active."""


def now():
    """The current local time with its UTC offset, e.g. 2026-10-14T10:00:00+05:30."""
    return datetime.now().astimezone().replace(microsecond=0)


def round_tag(round_id):
    """The Brevo tag put on every email of a round, e.g. coinedition-round-2026-10-14."""
    return ROUND_TAG_PREFIX + round_id


def _explain(path, error):
    if isinstance(error, PermissionError):
        return (f"{path.name} is open in another program (Excel?) or is read-only. "
                "Close it and run the script again")
    return f"{path.name} could not be written: {error}"


class SendLog:
    """The records of logs/production_send_log.csv, plus the rules that read them."""

    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        if self.path.exists():
            self._load()

    def _load(self):
        try:
            text = self.path.read_bytes().decode("utf-8-sig")
        except OSError as error:
            raise SendLogError(f"{self.path.name} could not be read: {error}") from None
        except UnicodeDecodeError:
            raise SendLogError(f"{self.path.name} is damaged (it was probably saved by another program). "
                               "Restore the newest copy from backups/send_log/") from None
        if not text.strip():
            return
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != LOG_COLUMNS:
            raise SendLogError(f"{self.path.name} has unexpected columns {reader.fieldnames} - it may be damaged. "
                               "Restore the newest copy from backups/send_log/")
        for row in reader:
            status = (row.get("status") or "").strip()
            try:
                when = datetime.fromisoformat(row.get("time") or "")
            except ValueError:
                when = None
            if status not in STATUSES or when is None:
                raise SendLogError(f"{self.path.name} line {reader.line_num} is damaged ({row}). "
                                   "Restore the newest copy from backups/send_log/")
            if when.tzinfo is None:
                when = when.astimezone()
            self.records.append({"time": when, "round": row.get("round") or "", "batch": row.get("batch") or "",
                                 "email": (row.get("email") or "").strip().lower(), "status": status,
                                 "detail": row.get("detail") or ""})

    # ---- writing --------------------------------------------------------------------------
    def check_writable(self):
        """Stop early (with a clear message) if the log can't be written - e.g. it is open in Excel."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8"):
                pass
        except OSError as error:
            raise SendLogError(_explain(self.path, error)) from None

    def append(self, round_id, batch, email, status, detail="", when=None):
        """Add one line and make sure it is on the disk before returning."""
        assert status in STATUSES
        when = when or now()
        row = [when.isoformat(), round_id, batch, email, status, " ".join(str(detail).split())[:500]]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(LOG_COLUMNS)
                writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())
        except OSError as error:
            raise SendLogError(_explain(self.path, error)) from None
        self.records.append({"time": when, "round": round_id, "batch": batch, "email": email,
                             "status": status, "detail": row[5]})

    # ---- rounds ---------------------------------------------------------------------------
    def rounds(self):
        found = []
        for r in self.records:
            if r["status"] == "round-start" and r["round"] not in found:
                found.append(r["round"])
        return found

    def current_round(self):
        rounds = self.rounds()
        return rounds[-1] if rounds else None

    def round_started(self, round_id):
        return next(r["time"] for r in self.records if r["status"] == "round-start" and r["round"] == round_id)

    def new_round_id(self, today):
        """Today's date, or today's date with -2, -3 ... if a round already started today."""
        used, base, n = set(self.rounds()), today.isoformat(), 1
        name = base
        while name in used:
            n += 1
            name = f"{base}-{n}"
        return name

    # ---- per-address state in a round -----------------------------------------------------
    def states(self, round_id):
        """{email: {"state", "batch", "detail", "failures", "counted_failures", "last_attempt"}}

        state: "sent" | "unknown" | "rejected" | "failed" (may be tried again)
        A "pending" line without a result after it -> "unknown" (the script stopped mid-send).
        counted_failures: refusals after which OTHER emails of this round still went through
        within an hour - evidence that the problem is this address, not the account.
        """
        records = [r for r in self.records if r["round"] == round_id]
        states = {}
        for index, r in enumerate(records):
            if r["status"] not in RESULTS | {"pending"}:
                continue
            s = states.setdefault(r["email"], {"state": None, "batch": r["batch"], "detail": "", "failures": 0,
                                               "counted_failures": 0, "last_attempt": None, "open": False})
            if r["batch"]:
                s["batch"] = r["batch"]
            if r["status"] == "pending":
                s["open"] = True
                s["last_attempt"] = r["time"]
                continue
            s["open"] = False
            s["detail"] = r["detail"]
            if s["state"] != "sent":
                s["state"] = r["status"]
            if r["status"] == "failed":
                s["failures"] += 1
                # Did another email of this round go through AFTER this refusal (later in the log,
                # within the hour)? Then the problem was this address, not the account.
                limit = r["time"] + OTHERS_WENT_THROUGH_WITHIN
                if any(o["status"] == "sent" and o["email"] != r["email"] and r["time"] <= o["time"] <= limit
                       for o in records[index + 1:]):
                    s["counted_failures"] += 1
        for s in states.values():
            if s.pop("open") and s["state"] != "sent":
                s["state"] = "unknown"
                s["detail"] = "the script stopped while this email was being handed to Brevo"
        return states

    # ---- counting emails that may have reached Brevo --------------------------------------
    def possibly_accepted(self):
        """The time of every email that Brevo accepted or MAY have accepted - each person counted
        once per round. Refused emails (failed / rejected) did not use any of Brevo's credits."""
        first_time, open_pending = {}, {}
        for r in self.records:
            key = (r["round"], r["email"])
            if r["status"] == "pending":
                if key in open_pending:  # an earlier attempt never got a result: it may have gone out
                    first_time.setdefault(key, open_pending[key])
                open_pending[key] = r["time"]
            elif r["status"] in RESULTS:
                started = open_pending.pop(key, None)
                if r["status"] in ("sent", "unknown"):
                    first_time.setdefault(key, started or r["time"])
        for key, started in open_pending.items():
            first_time.setdefault(key, started)
        return list(first_time.values())

    def accepted_since(self, since):
        return sum(1 for t in self.possibly_accepted() if t >= since)

    def accepted_between_days(self, first_day, last_day):
        return sum(1 for t in self.possibly_accepted() if first_day <= t.date() <= last_day)


class SendLock:
    """Only one sending run at a time (production AND test sends use it).

    The lock is held by the operating system on logs/send.lock and is released automatically
    when the process ends - even after a crash - so a leftover file can never block sending.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise LockedError("another sending run (send_emails.py or send_test.py) is still running. "
                              "Wait for it to finish, then try again") from None
        return self

    def __exit__(self, *exc):
        if self.file:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.file.close()
            self.file = None
        return False


def backup_log(path, backup_dir, label, keep=60):
    """Copy the send log to backups/send_log/ (kept outside logs/ on purpose). Returns the copy."""
    path, backup_dir = Path(path), Path(backup_dir)
    if not path.exists() or path.stat().st_size == 0:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"production_send_log_{now():%Y-%m-%d_%H%M%S}_{label}.csv"
    shutil.copy2(path, target)
    for old in sorted(backup_dir.glob("production_send_log_*.csv"))[:-keep]:
        old.unlink()
    return target
