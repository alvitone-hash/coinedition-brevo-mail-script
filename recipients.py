"""
recipients.py - Email addresses and recipient files, shared by split_excel.py,
send_test.py and send_emails.py so all three clean and read addresses the same way.

  clean_email() / is_valid_email()   tidy and check one address
  read_table()                       read a .csv or .xlsx file -> (headers, rows)
  find_column()                      match "Email", "E-mail Address", ... to a real header
  status_columns()                   columns that look like a subscription status
"""

import csv
import io
import re
from pathlib import Path


EMAIL_PATTERN = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"  # before the @, e.g. john.smith
    r"@([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$"                    # after the @, e.g. gmail.com
)

# Column headers accepted for each value (not case-sensitive, spaces/dashes/underscores ignored).
COLUMN_NAMES = {
    "email": ["email", "e-mail", "email address", "e-mail address", "email_address", "mail"],
    "name": ["name", "first name", "firstname", "first_name", "given name"],
    "surname": ["surname", "last name", "lastname", "last_name", "family name"],
}

# Headers that suggest the file says WHO may be emailed (subscribed / unsubscribed / bounced ...).
STATUS_COLUMN = re.compile(r"status|subscri|opt.?in|opt.?out|consent|blacklist|blocklist|bounce|unsub",
                           flags=re.IGNORECASE)


def clean_email(value):
    """Tidy an email address: remove spaces, hidden characters and 'mailto:', make it lower-case."""
    # chr(0xA0) = non-breaking space, chr(0x200B) = zero-width space (both sneak in when copying from web pages)
    email = str(value or "").replace(chr(0xA0), " ").replace(chr(0x200B), "").replace(chr(0xFEFF), "")
    email = email.strip().strip("<>\"'").strip()
    if email.lower().startswith("mailto:"):
        email = email[len("mailto:"):]
    return email.strip().lower()


def is_valid_email(email):
    """True if a cleaned email address looks valid, e.g. 'name@example.com'."""
    if not 0 < len(email) <= 254 or EMAIL_PATTERN.match(email) is None:
        return False
    return len(email.split("@", 1)[0]) <= 64  # the part before the @ may have 64 characters at most


def read_text(path):
    """Read a text file saved as UTF-8 (VS Code, Notepad, 'CSV UTF-8') or by Excel's plain 'CSV' option."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def _normalise(header):
    return re.sub(r"[\s_\-]+", " ", str(header or "").replace(chr(0xFEFF), "")).strip().lower()


def find_column(headers, what):
    """The real header for "email", "name" or "surname" (see COLUMN_NAMES), or None."""
    accepted = {_normalise(name) for name in COLUMN_NAMES[what]}
    for header in headers:
        if _normalise(header) in accepted:
            return header
    return None


def status_columns(headers):
    """Headers that look like a subscription status (e.g. Status, Subscribed, Opt-in, Unsubscribed)."""
    return [h for h in headers if h and STATUS_COLUMN.search(str(h))]


def read_table(path):
    """Read a subscriber file -> (headers, rows, description).

    .csv : UTF-8 or Windows encoding; the separator (, ; tab or |) is detected.
    .xlsx: the first sheet.
    Every cell is read as text exactly as written - "NA", "None" or "0012" stay as they are.
    Rows are dicts {header: text}. Completely empty rows are kept (the caller counts them).
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = read_text(path)
        sample = text[:20000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        table = [row for row in reader]
        if not table:
            return [], [], f"CSV file, separator {delimiter!r}, empty"
        headers = [str(h).replace(chr(0xFEFF), "").strip() for h in table[0]]
        rows = []
        for cells in table[1:]:
            cells = cells + [""] * (len(headers) - len(cells))
            rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(headers) if h})
        name = {",": "comma", ";": "semicolon", "\t": "tab", "|": "|"}.get(delimiter, delimiter)
        return [h for h in headers if h], rows, f"CSV file, {name}-separated"
    if suffix in (".xlsx", ".xlsm"):
        import pandas as pd  # only needed for Excel files
        sheet = pd.read_excel(path, dtype=str, engine="openpyxl", keep_default_na=False, na_filter=False)
        headers = [str(h).strip() for h in sheet.columns]
        sheet.columns = headers
        rows = [{h: ("" if v is None else str(v)) for h, v in record.items()}
                for record in sheet.to_dict(orient="records")]
        return headers, rows, "Excel file (first sheet)"
    raise ValueError(f"{path.name}: only .csv and .xlsx files can be read")
