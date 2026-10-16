"""
newsletter_params.py - Build the Brevo "params" from newsletter/newsletter_data.txt.

send_test.py AND send_emails.py both use this file, so the test emails and the
production emails are built in exactly the same way:

    newsletter/newsletter_data.txt          KEY=value lines (prices, stories, URLs ...)
            |  read_newsletter_data()
            v
    check_newsletter_data()                 check_values(): required values present, numbered entries
            |                               complete, URLs, colours, markup - plus readiness_problems():
            |                               no sample content, READY_TO_SEND_ON = today (only a TEST may
            |                               skip these, with send_test.py --allow-sample-content)
            v
    build_params(data, first_name)          {"btc_price": ..., "trending_rows": ..., ...}
    email_subject(data)                     the subject line = STORY_1_TITLE (first Top Stories heading)
            |
            v
    Brevo REST API  POST /v3/smtp/email     {"templateId": 1, "subject": ..., "params": {...}}

Template 1 (template/brevo_template_1.html) holds the design, with every value written as
{{ params.name }}. The images (logo, hero banner, partner banner) are fixed in the template.

Sections with a changing number of entries
------------------------------------------
Top Stories, Trending Searches & Gainers and Also on the Radar are numbered in the data file:
STORY_1_TITLE, STORY_2_TITLE ..., TREND_1_NAME, TREND_2_NAME ..., RADAR_1_LABEL ... The file decides
how many entries each section has that day - 4 trending coins today, 6 tomorrow; neither this code nor
the template fixes the number. The email shows exactly the entries in the file, in number order.
The numbers must run 1, 2, 3 ... without gaps, and every entry needs its required values, otherwise
sending stops: nothing is renumbered, left out or made up. The rows are built here with the design's
own markup (SECTION MARKUP below, copied from the template with the same styles) and sent as four
ready-made HTML params: stories_before_partner (stories 1-2, above the partner banner),
stories_after_partner (story 3 onwards), trending_rows and radar_items.

How the values are kept safe
----------------------------
* Plain-text values (prices, the intro ...) are sent exactly as typed. Brevo HTML-escapes every
  {{ params.x }} itself, so & < > " ' appear exactly as typed and cannot break the email's HTML.
  They must NOT be escaped here as well, or readers would see "&amp;" instead of "&".
* The four section params are ready-made HTML that the template prints with |safe
  (SAFE_HTML_PARAMS), so every value in them is HTML-escaped HERE, the way Brevo would escape it.
  Story paragraphs (STORY_N_P1..P3) and radar texts (RADAR_N_TEXT) may use **bold**: the WHOLE text
  is escaped first, then only **...** becomes <strong>...</strong>, and the paragraphs of a story are
  joined with two line breaks (<br /><br />, as in the design). No other HTML, and never a script,
  can reach the email from the data file.
* Web addresses must start with https:// and are percent-encoded.
"""

import difflib
import html
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from requests.utils import requote_uri

PROJECT_DIR = Path(__file__).resolve().parent
DATA_FILE = PROJECT_DIR / "newsletter" / "newsletter_data.txt"


# =============================================================================
# Brevo template parameter  <-  key in newsletter_data.txt
# (first_name is not in this list: it comes from each recipient's Name. The
# numbered Top Stories, Trending and Radar entries are in SECTIONS below.)
# =============================================================================

PARAM_SOURCES = {
    # Newsletter details at the top. Optional: DATE empty -> today's date, the other
    # three empty -> the designer's wording (DEFAULT_TEXT).
    "date": "DATE",
    "newsletter_title": "NEWSLETTER_TITLE",
    "intro_heading": "INTRO_HEADING",
    "intro_description": "INTRO_DESCRIPTION",

    # Market snapshot. *_color is set from the sign of the change (see COLOR_PARAMS).
    "btc_price": "BTC_PRICE",
    "btc_change": "BTC_CHANGE",
    "btc_color": "BTC_COLOR",
    "eth_price": "ETH_PRICE",
    "eth_change": "ETH_CHANGE",
    "eth_color": "ETH_COLOR",
    "bnb_price": "BNB_PRICE",
    "bnb_change": "BNB_CHANGE",
    "bnb_color": "BNB_COLOR",
    "xrp_price": "XRP_PRICE",
    "xrp_change": "XRP_CHANGE",
    "xrp_color": "XRP_COLOR",
    "sol_price": "SOL_PRICE",
    "sol_change": "SOL_CHANGE",
    "sol_color": "SOL_COLOR",
    "global_cap": "GLOBAL_CAP",
    "global_change": "GLOBAL_CHANGE",
    "global_color": "GLOBAL_COLOR",

    # "Explore More on Coin Edition" links at the bottom of the email
    "xrp_news_url": "XRP_NEWS_URL",
    "india_crypto_news_url": "INDIA_CRYPTO_NEWS_URL",
    "crypto_hacks_url": "CRYPTO_HACKS_URL",
    "crypto_scams_url": "CRYPTO_SCAMS_URL",
    "michael_saylor_url": "MICHAEL_SAYLOR_URL",
    "donald_trump_url": "DONALD_TRUMP_URL",
    "changpeng_zhao_url": "CHANGPENG_ZHAO_URL",
    "vitalik_buterin_url": "VITALIK_BUTERIN_URL",

    # The recap line under "Explore More on Coin Edition" - the Coin Edition recap the issue is based on:
    # "Market figures and developments reflect the RECAP_DATE Coin Edition recap. Read the full report"
    # (the link goes to RECAP_URL). Both change with every issue.
    "recap_date": "RECAP_DATE",
    "recap_url": "RECAP_URL",
}

# Every fixed key the data file may contain (besides READY_TO_SEND_ON and the numbered entries).
DATA_KEYS = list(PARAM_SOURCES.values())


# =============================================================================
# SECTIONS whose number of entries changes from day to day
# =============================================================================
# One entry = the keys PREFIX_N_FIELD with the same number N, e.g. TREND_3_NAME + TREND_3_CHANGE.
# The data file decides how many entries a section has. They must be numbered 1, 2, 3 ... without
# gaps, and the email shows exactly those entries, in number order.
SECTION_FIELDS = {
    "STORY": ("TITLE", "P1", "P2", "P3", "URL"),   # headline, 1-3 paragraphs, article address
    "TREND": ("NAME", "CHANGE", "COLOR"),          # coin, 24-hour change, its colour (follows the sign)
    "RADAR": ("LABEL", "TEXT"),                    # bold heading, one or two sentences
}
REQUIRED_FIELDS = {"STORY": ("TITLE", "P1"), "TREND": ("NAME", "CHANGE"), "RADAR": ("LABEL", "TEXT")}
RICH_TEXT_FIELDS = {"STORY": ("P1", "P2", "P3"), "TREND": (), "RADAR": ("TEXT",)}   # **bold** allowed
SECTION_TITLES = {"STORY": "Top Stories", "TREND": "Trending Searches & Gainers", "RADAR": "Also on the Radar"}
ENTRY_KEY = re.compile(r"^(STORY|TREND|RADAR)_([1-9][0-9]*)_([A-Z0-9]+)$")

# The section params: the rows of each section as ready-made HTML (SECTION MARKUP below), printed in
# the template with |safe. The partner banner sits between stories 2 and 3, as in the design.
SECTION_PARAMS = {
    "stories_before_partner": "STORY",
    "stories_after_partner": "STORY",
    "trending_rows": "TREND",
    "radar_items": "RADAR",
}
SAFE_HTML_PARAMS = list(SECTION_PARAMS)
STORIES_ABOVE_PARTNER = 2

# What the email shows when the title / intro keys are empty: the designer's wording.
DEFAULT_TEXT = {
    "newsletter_title": "DAILY CRYPTO MARKET RECAP",
    "intro_heading": "24-Hour Crypto Recap",
    "intro_description": "Here’s what happened across the crypto market over the past 24 hours.",
}

# Key names used before (and in the designer's first draft of the template). They are still
# accepted and read as the new name, so an older file keeps working - the numbered ones for any number.
OLD_KEY_NAMES = {
    "GLOBAL_MARKET_CAP": "GLOBAL_CAP",
    "GLOBAL_MARKET_CHANGE": "GLOBAL_CHANGE",
    "GLOBAL_MARKET_CAP_CHANGE": "GLOBAL_CHANGE",
}
OLD_ENTRY_NAMES = [
    (re.compile(r"^TRENDING_([1-9][0-9]*)_(NAME|CHANGE)$"), r"TREND_\1_\2"),
    (re.compile(r"^STORY_([1-9][0-9]*)_DESCRIPTION$"), r"STORY_\1_P1"),
    (re.compile(r"^RADAR_([1-9][0-9]*)_TITLE$"), r"RADAR_\1_LABEL"),
    (re.compile(r"^RADAR_([1-9][0-9]*)_DESCRIPTION$"), r"RADAR_\1_TEXT"),
]

# Keys of an earlier version that are no longer used: the images are fixed in the template.
RETIRED_KEYS = {"LOGO_IMAGE_URL", "HERO_IMAGE_URL", "PARTNER_IMAGE_URL", "PARTNER_LINK", "PARTNER_ALT",
                "STORY_1_IMAGE_URL", "STORY_2_IMAGE_URL"}

# Not a template value: your confirmation that the content is real and meant for today.
READY_KEY = "READY_TO_SEND_ON"
CONTROL_KEYS = {READY_KEY}

# Params that hold a web address (the Explore links and the recap link). They are checked by url_problem()
# and encoded by clean_url() - and so is every STORY_N_URL, which goes into the Top Stories rows.
URL_PARAMS = [
    "xrp_news_url", "india_crypto_news_url", "crypto_hacks_url", "crypto_scams_url",
    "michael_saylor_url", "donald_trump_url", "changpeng_zhao_url", "vitalik_buterin_url", "recap_url",
]
# Sites that only serve dummy pages or dummy pictures - never real content.
PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "placehold.co", "placeholder.com",
                     "dummyimage.com", "picsum.photos", "placekitten.com", "lorempixel.com")

# Text colour of each 24-hour change in the market snapshot ({{ params.btc_color }} etc.) <- the change
# it follows. Each trending row's pill is coloured the same way, from its TREND_N_CHANGE.
COLOR_PARAMS = {
    "btc_color": "BTC_CHANGE",
    "eth_color": "ETH_CHANGE",
    "bnb_color": "BNB_CHANGE",
    "xrp_color": "XRP_CHANGE",
    "sol_color": "SOL_CHANGE",
    "global_color": "GLOBAL_CHANGE",
}
RISE_COLOR = "#0B7A44"   # the design's "Positive" colour (also used for trending coins)
FALL_COLOR = "#C8102E"   # the design's "Negative" colour
FLAT_COLOR = "#7A8396"   # the design's "Muted text" colour, for a change of exactly 0%
DIRECTION_COLORS = {"up": RISE_COLOR, "down": FALL_COLOR, "flat": FLAT_COLOR}
COLOR_WORDS = {"green": "up", "red": "down", "grey": "flat", "gray": "flat"}
MINUS_SIGNS = ("-", "−", "–")  # a hyphen, or the minus / dash signs copied from websites

# A 24-hour change such as +0.35%, -0.85%, -1,250.5% or 0.00%
CHANGE_PATTERN = re.compile(r"^([+\-−–])?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*%$")
PRICE_KEYS = ["BTC_PRICE", "ETH_PRICE", "BNB_PRICE", "XRP_PRICE", "SOL_PRICE", "GLOBAL_CAP"]

# Fixed keys that may be left empty. Everything else is shown in the design, so it is required.
# (Of the numbered entries, the fields not in REQUIRED_FIELDS may be empty: STORY_N_P2, STORY_N_P3,
# STORY_N_URL - the story titles are no longer links, but a filled-in address is still checked -
# and TREND_N_COLOR.)
OPTIONAL_KEYS = {
    "DATE", "NEWSLETTER_TITLE", "INTRO_HEADING", "INTRO_DESCRIPTION",
    "BTC_COLOR", "ETH_COLOR", "BNB_COLOR", "XRP_COLOR", "SOL_COLOR", "GLOBAL_COLOR",
}

# Values must be plain text: HTML tags and codes like &amp; are refused (use **bold** instead).
HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*\b[^<>]*>")
HTML_CODE = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
BOLD = re.compile(r"\*\*(.+?)\*\*")
# Words that mark sample or placeholder content, which must never reach subscribers.
SAMPLE_MARKERS = re.compile(r"\bsample\b|lorem ipsum|\bYOUR_[A-Z0-9_]+|\?REPLACE=", flags=re.IGNORECASE)


# =============================================================================
# READ, CHECK AND BUILD
# =============================================================================

def clean_text(value):
    """A value without surrounding spaces or invisible characters."""
    # chr(0x200B) = zero-width space, chr(0xFEFF) = byte-order mark (both sneak in when copying from web pages)
    return value.replace(chr(0x200B), "").replace(chr(0xFEFF), "").strip()


def current_key_name(written):
    """The name this version uses for a key: an old name (OLD_KEY_NAMES, OLD_ENTRY_NAMES) becomes the new one."""
    if written in OLD_KEY_NAMES:
        return OLD_KEY_NAMES[written]
    for pattern, new_name in OLD_ENTRY_NAMES:
        if pattern.match(written):
            return pattern.sub(new_name, written)
    return written


def read_newsletter_data(path=DATA_FILE):
    """Read the KEY=value lines of newsletter_data.txt. Returns (data, problems).

    Everything after the first "=" is the value, so URLs containing "=" are fine.
    Lines starting with # and blank lines are ignored. Keys are not case-sensitive,
    and old key names are read as their new names (current_key_name).
    The file is read fresh every time, so an edit is used by the very next run.
    """
    if not path.exists():
        return {}, [f"{path} not found"]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1252")  # saved by Notepad/Excel in the Windows format

    data = {}
    written_as = {}  # key -> the name it was written with in the file
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = clean_text(line)
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {number} has no '=': {line[:60]}  (each value must stay on ONE line -"
                            " did a long text wrap onto a new line?)")
            continue
        key, value = line.split("=", 1)
        written = key.strip().upper()
        key = current_key_name(written)
        if not key:
            problems.append(f"line {number} has no key name before the '='")
            continue
        if key in data:
            also = f" (also written as {written_as[key]})" if written_as[key] != written else ""
            problems.append(f"line {number}: {key} is in the file twice{also} - keep only one")
        written_as[key] = written
        data[key] = clean_text(value)
    return data, problems


# ---- numbered entries (Top Stories, Trending, Radar) ------------------------

def entry_key_parts(key):
    """("TREND", 3, "NAME") for TREND_3_NAME - or None if the key is not a field of a numbered entry."""
    match = ENTRY_KEY.match(key)
    if match and match.group(3) in SECTION_FIELDS[match.group(1)]:
        return match.group(1), int(match.group(2)), match.group(3)
    return None


def entry_numbers(data, prefix):
    """The numbers of a section's entries in the file, in order: [1, 2, 3, 4] for TREND_1_... to TREND_4_..."""
    return sorted({parts[1] for parts in map(entry_key_parts, data) if parts and parts[0] == prefix})


def entries(data, prefix):
    """A section's entries in number order - each a dict of its fields (a missing one is "") plus "number"."""
    return [dict({field: data.get(f"{prefix}_{n}_{field}", "") for field in SECTION_FIELDS[prefix]}, number=n)
            for n in entry_numbers(data, prefix)]


def optional_keys(data):
    """Every key that may be left empty: OPTIONAL_KEYS plus the optional fields of the entries in the file."""
    return OPTIONAL_KEYS | {f"{prefix}_{n}_{field}" for prefix, fields in SECTION_FIELDS.items()
                            for n in entry_numbers(data, prefix)
                            for field in fields if field not in REQUIRED_FIELDS[prefix]}


def is_rich_text_key(key):
    """True for the keys that may use **bold**: STORY_N_P1..P3 and RADAR_N_TEXT."""
    parts = entry_key_parts(key)
    return bool(parts) and parts[2] in RICH_TEXT_FIELDS[parts[0]]


def change_color_keys(data):
    """(COLOR key, CHANGE key) of every 24-hour change shown with a colour: the market snapshot and
    every trending row in the file."""
    pairs = [(PARAM_SOURCES[param], change_key) for param, change_key in COLOR_PARAMS.items()]
    return pairs + [(f"TREND_{n}_COLOR", f"TREND_{n}_CHANGE") for n in entry_numbers(data, "TREND")]


def number_ranges(prefix, numbers):
    """Entry numbers in words: [1, 2, 4] -> "TREND_1 to TREND_2, TREND_4"."""
    runs = []
    for n in numbers:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"{prefix}_{a}" + (f" to {prefix}_{b}" if b > a else "") for a, b in runs)


def entry_problems(data):
    """Why the numbered entries can't be shown exactly as the file has them: a section without entries,
    a gap in the numbering, or an entry without its required values."""
    problems = []
    for prefix, required in REQUIRED_FIELDS.items():
        section, numbers = SECTION_TITLES[prefix], entry_numbers(data, prefix)
        if not numbers:
            problems.append(f"{section} has no entries - add at least "
                            + " and ".join(f"{prefix}_1_{field}" for field in required))
            continue
        missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
        if missing:
            problems.append(f"{section}: the entries must be numbered 1, 2, 3 ... without gaps, but "
                            f"{number_ranges(prefix, missing)} {'is' if len(missing) == 1 else 'are'} missing "
                            f"(the file has {number_ranges(prefix, numbers)}) - renumber them in the file; "
                            "nothing is renumbered or left out automatically")
        for entry in entries(data, prefix):
            n = entry["number"]
            if not any(entry[field] for field in SECTION_FIELDS[prefix]):
                delete = (f", or delete the {prefix}_{n} lines if {section} has only {n - 1} today"
                          if n > 1 and n == numbers[-1] else "")
                problems.append(f"{prefix}_{n} has lines in the file but no values - fill in "
                                + " and ".join(f"{prefix}_{n}_{field}" for field in required) + delete)
                continue
            for field in required:
                key = f"{prefix}_{n}_{field}"
                if key not in data:
                    problems.append(f"{key} is required, but its line is missing from the file")
                elif not data[key]:
                    problems.append(f"{key} is required, but it is empty")
    return problems


def key_suggestions(data, key):
    """The keys a mistyped key may have meant: the fixed ones, and the fields of entry 1, of the entries in
    the file and of the number written in the key itself (TREND_01_NAME -> TREND_1_NAME)."""
    numbers = {int(n) for n in re.findall(r"\d+", key)} | {1}
    numbers |= {n for prefix in SECTION_FIELDS for n in entry_numbers(data, prefix)}
    return DATA_KEYS + sorted(CONTROL_KEYS) + [f"{prefix}_{n}_{field}" for prefix, fields in SECTION_FIELDS.items()
                                               for n in sorted(numbers) if n > 0 for field in fields]


# ---- single values ------------------------------------------------------------

def url_problem(value):
    """Why a web address can't be used (a short message), or None if it is fine."""
    url = value.strip()
    if url.lower().startswith("http://"):
        return "must start with https:// (not http://)"
    if not re.match(r"^https://[^/\s?#]+\.[^/\s?#]+", url, flags=re.IGNORECASE):
        return "is not a web address - it must start with https:// followed by a domain name"
    if any(char in url for char in '"<>`\\') or any(ord(char) < 32 for char in url):
        return 'contains a character that is not allowed in a web address (" < > ` or \\)'
    if "&amp;" in url.lower():
        return "contains &amp; - write a plain & instead"
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "is not a valid web address"
    if any(host == name or host.endswith("." + name) for name in PLACEHOLDER_HOSTS):
        return "is a placeholder address, not a real page"
    return None


def clean_url(value):
    """A safe, properly encoded web address - or None if it can't be used (see url_problem()).

    Spaces and accented characters are percent-encoded (a space becomes %20), and an
    address that is already encoded is left as it is. The URL is NOT HTML-escaped here:
    it travels to Brevo as plain JSON text and Brevo escapes it when it fills href="...".
    """
    if url_problem(value):
        return None
    return requote_uri(value.strip())


def parse_change(change):
    """Read a 24-hour change such as "+0.35%" -> ("up", None), "-0.85%" -> ("down", None),
    "0.00%" -> ("flat", None). Returns (None, reason) when the value can't be used."""
    match = CHANGE_PATTERN.match(change.strip())
    if not match:
        return None, "is not a percentage like +0.35% or -0.85%"
    sign, whole, fraction = match.groups()
    number = float(whole.replace(",", "") + "." + (fraction or "0"))
    if number == 0:
        return "flat", None
    if sign is None:
        return None, "needs a + or - sign, e.g. +0.35% or -0.85% (only 0% may be written without one)"
    return ("down" if sign in MINUS_SIGNS else "up"), None


def is_negative(change):
    """True if a change such as "-0.85%" is negative (also with a minus sign copied from a website)."""
    return parse_change(change)[0] == "down"


def color_value(color, change):
    """The CSS colour for a 24-hour change: green when it rose, red when it fell, grey for 0%.

    *_COLOR may be left empty (recommended). If it is filled in (green, red or grey), it
    must agree with the sign of the change - check_newsletter_data() refuses it otherwise,
    so the text and its colour can never disagree.
    """
    direction = parse_change(change)[0]
    if direction is None:  # unreadable change - check_newsletter_data() reports it
        direction = COLOR_WORDS.get(color.strip().lower(), "flat")
    return DIRECTION_COLORS[direction]


def rich_text_problem(value):
    """Why a story paragraph / radar text can't be used (a short message), or None."""
    if "****" in value:
        return "contains **** - put the bold words between the ** marks, e.g. **3x leverage**"
    if value.count("**") % 2:
        return "has an odd number of ** - every bold part needs ** before AND after it, e.g. **$3.8 million**"
    return None


def rich_text_html(value):
    """Plain text with optional **bold** -> safe HTML: everything is escaped, then only
    **...** becomes <strong>...</strong>. Nothing else from the data file can become HTML."""
    escaped = html.escape(value, quote=False)
    if rich_text_problem(value):
        return escaped
    return BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)


def check_newsletter_data(data, today=None):
    """Check the loaded data. Returns (problems, warnings) - problems must be fixed before sending.

    This is the full check production (send_emails.py) uses: readiness_problems() - the content is
    confirmed for today and holds no sample values - plus check_values()."""
    problems, warnings = check_values(data)
    return readiness_problems(data, today) + problems, warnings


def readiness_problems(data, today=None):
    """Why the content is NOT confirmed as real and current for today: READY_TO_SEND_ON is empty, not a
    date or another day's date, or a value is still sample/placeholder content.

    Production always refuses these (check_newsletter_data). Only a TEST send with
    send_test.py --allow-sample-content may go ahead with them - and labels every email as unverified."""
    today = today or date.today()
    problems = []

    # ---- your confirmation that the content is real and for today ----------
    ready = data.get(READY_KEY, "")
    if READY_KEY not in data or not ready:
        problems.append(f"{READY_KEY} is empty, so the content is NOT confirmed as real and current "
                        f"(the file still holds sample values). After entering today's real prices, stories, "
                        f"trending coins and radar items, set {READY_KEY}={today.isoformat()}")
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ready):
        problems.append(f"{READY_KEY}={ready} must be a date written like {today.isoformat()}")
    elif ready != today.isoformat():
        problems.append(f"{READY_KEY}={ready}, but today is {today.isoformat()}. Update the prices, stories, "
                        f"trending coins and radar items for today, then set {READY_KEY}={today.isoformat()}")

    for key, value in data.items():
        marker = SAMPLE_MARKERS.search(value)
        if marker:
            problems.append(f"{key} is still sample/placeholder content ('{marker.group()}') - replace it with "
                            "real content before sending")
    return problems


def check_values(data):
    """Every check that does not depend on the day: required values present, numbered entries complete,
    web addresses, colours, markup, prices and changes. Returns (problems, warnings). Applies to test AND
    production emails."""
    problems = []
    warnings = []

    for key in DATA_KEYS:
        if key in OPTIONAL_KEYS:
            continue
        if key not in data:
            problems.append(f"{key} is required, but its line is missing from the file")
        elif not data[key]:
            problems.append(f"{key} is required, but it is empty")
    problems += entry_problems(data)

    entry_keys = [key for key in data if entry_key_parts(key)]
    url_keys = [PARAM_SOURCES[p] for p in URL_PARAMS] + [key for key in entry_keys if entry_key_parts(key)[2] == "URL"]
    for key in url_keys:
        value = data.get(key, "")
        problem = url_problem(value) if value else None
        if problem:
            problems.append(f"{key} {problem}: {value}")

    color_pairs = change_color_keys(data)
    color_keys = {color_key for color_key, _ in color_pairs}
    for key in DATA_KEYS + entry_keys:
        value = data.get(key, "")
        if not value or key in url_keys or key in color_keys:
            continue
        tag = HTML_TAG.search(value)
        code = HTML_CODE.search(value)
        if tag:
            problems.append(f"{key} contains the HTML tag {tag.group()} - write plain text only "
                            "(use **double asterisks** for bold in story and radar texts)")
        elif code:
            problems.append(f"{key} contains the HTML code {code.group()} - type the character itself "
                            "instead, e.g. & or ’")
        elif is_rich_text_key(key) and rich_text_problem(value):
            problems.append(f"{key} {rich_text_problem(value)}")
        elif not is_rich_text_key(key) and "**" in value:
            problems.append(f"{key} contains ** - bold is only possible in story paragraphs and radar texts")

    for key in PRICE_KEYS:
        value = data.get(key, "")
        if value and not re.search(r"\d", value):
            problems.append(f"{key}={value} has no number in it - write the figure, e.g. $84,612.30")

    recap_date, issue_date = data.get("RECAP_DATE", ""), data.get("DATE", "")
    if recap_date and not re.search(r"\d", recap_date):
        problems.append(f"RECAP_DATE={recap_date} has no number in it - write the recap's date, e.g. October 15, 2026")
    elif recap_date and issue_date and recap_date not in issue_date:
        warnings.append(f"RECAP_DATE={recap_date} is not the date of this issue ({issue_date}) - check that the recap "
                        "line under Explore More names and links the right day's recap (RECAP_DATE, RECAP_URL)")

    # Every change with a colour - the market snapshot and each trending row - needs its sign,
    # because the colour follows it; rises and falls are both fine.
    for color_key, change_key in color_pairs:
        change = data.get(change_key, "")
        if not change:
            continue  # reported above as a missing required value
        direction, reason = parse_change(change)
        if reason:
            problems.append(f"{change_key}={change} {reason}")
            continue
        color = data.get(color_key, "").strip().lower()
        if color and color not in COLOR_WORDS:
            problems.append(f"{color_key}={data[color_key]} is not allowed - leave it empty (recommended) so the "
                            f"colour follows the sign of {change_key}, or write green, red or grey")
        elif color and COLOR_WORDS[color] != direction:
            problems.append(f"{color_key}={data[color_key]} disagrees with {change_key}={change} - leave "
                            f"{color_key} empty so the colour always matches the change")

    for key, value in data.items():
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            warnings.append(f"{key} is wrapped in quotes - the quotes will appear in the email")

    for key in data:
        if key in RETIRED_KEYS:
            warnings.append(f"{key} is no longer used - the images are fixed in the template, as in the "
                            "original design (the line can be deleted)")
        elif key not in DATA_KEYS and key not in CONTROL_KEYS and not entry_key_parts(key):
            guess = difflib.get_close_matches(key, key_suggestions(data, key), n=1, cutoff=0.8)
            hint = f" - did you mean {guess[0]}?" if guess else " (a typo?)"
            if key.startswith(tuple(SECTION_FIELDS)):
                # Looks like a story / trending / radar line, but the email would leave it out.
                prefix = next(p for p in SECTION_FIELDS if key.startswith(p))
                example = ", ".join(f"{prefix}_{n}_{field}" for n in (1, 2) for field in REQUIRED_FIELDS[prefix])
                problems.append(f"{key} is not a key the newsletter knows{hint} - as written, this line would be "
                                f"left out of the email. Entries are numbered from 1 without gaps: {example} ...")
            else:
                warnings.append(f"{key} is not used by the template{hint}")
    return problems, warnings


def format_date(day):
    """A date as newsletter text, e.g. 'Thursday, October 8, 2026'."""
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"


# =============================================================================
# SECTION MARKUP - the rows of the numbered sections, with the design's own HTML
# =============================================================================
# Copied from template/brevo_template_1.html as it was live in Brevo on 2026-10-14: the same tags,
# attributes and inline styles, so the email looks exactly as before - only the number of rows
# follows the data file. The values arrive already HTML-escaped (esc(), rich_text_html()).

STORY_ROW = """\
              <!-- ===== STORY {number} ===== -->
              <tr>
                <td class="px" style="padding:{padding};">
                  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%">
                    <tr>
                      <!-- number badge -->
                      <td width="42" valign="top" style="width:42px; padding:0;">
                        <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="28" style="width:28px; border-collapse:separate;">
                          <tr>
                            <td align="center" height="28" style="width:28px; height:28px; background-color:#F2F2F2; border-radius:14px; font-family:Arial,Helvetica,sans-serif; font-size:14px; line-height:28px; font-weight:bold; color:#181C47; mso-line-height-rule:exactly;">
                              {number}
                            </td>
                          </tr>
                        </table>
                      </td>
                      <td valign="top" style="padding:0;">
                        <!-- Story title, not a link (the article links were removed on request); the article address is kept only in the invisible data-story-url attribute. -->
                        <div class="h3" data-story-url="{url}" style="font-family:Arial,Helvetica,sans-serif; font-size:19px; line-height:27px; font-weight:bold; color:#181C47; letter-spacing:-0.2px; mso-line-height-rule:exactly;">
                          {title}
                        </div>
                        <div class="body-text" style="padding-top:10px; font-family:Arial,Helvetica,sans-serif; font-size:16px; line-height:26px; color:#3C4455; mso-line-height-rule:exactly;">
                          {description}
                        </div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>"""

STORY_HAIRLINE_ROW = """\
              <!-- hairline between stories -->
              <tr>
                <td class="px" style="padding:24px 36px 0 36px;">
                  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%">
                    <tr><td height="1" style="height:1px; line-height:1px; font-size:0; background-color:#EEEEEE;">&nbsp;</td></tr>
                  </table>
                </td>
              </tr>"""

# With fewer than 3 stories the partner banner is the last row of Top Stories: this gives it the
# 32px the last story would otherwise leave below the section.
PARTNER_SPACER_ROW = """\
              <!-- space under the partner banner (no story below it today) -->
              <tr><td height="32" style="height:32px; line-height:32px; font-size:0;">&nbsp;</td></tr>"""

TREND_ROW = """\
                    <!-- Trending {number}{last_note} -->
                    <tr>
                      <td class="cell-sm" style="padding:13px 16px; {name_edge} font-family:Arial,Helvetica,sans-serif; font-size:15px; line-height:22px; font-weight:500; color:#181C47; mso-line-height-rule:exactly;">
                        {name}
                      </td>
                      <td align="right" class="cell-sm" style="padding:13px 16px; {change_edge}">
                        <table role="presentation" border="0" cellpadding="0" cellspacing="0" align="right" style="border-collapse:separate;">
                          <tr>
                            <td class="pill" style="padding:4px 12px; border:1px solid #E5E5E5; border-radius:13px; font-family:Arial,Helvetica,sans-serif; font-size:14px; line-height:20px; font-weight:500; color:{color}; mso-line-height-rule:exactly;">
                              {change}
                            </td>
                          </tr>
                        </table>
                      </td>
                    </tr>"""
TREND_HAIRLINE = "border-bottom:1px solid #EEEEEE;"   # under every trending row but the last

RADAR_PANEL = """\
                    <!-- Radar {number} -->
                    <tr>
                      <td style="padding:16px 18px; border:1px solid #E5E5E5; border-radius:10px;">
                        <div style="font-family:Arial,Helvetica,sans-serif; font-size:15px; line-height:22px; font-weight:bold; color:#181C47; mso-line-height-rule:exactly;">
                          {label}
                        </div>
                        <div class="body-text" style="padding-top:6px; font-family:Arial,Helvetica,sans-serif; font-size:15px; line-height:25px; color:#3C4455; mso-line-height-rule:exactly;">
                          {text}
                        </div>
                      </td>
                    </tr>{gap}"""
RADAR_GAP_ROW = """
                    <tr><td height="12" style="height:12px; line-height:12px; font-size:0;">&nbsp;</td></tr>"""


def esc(value):
    """A plain-text value inside the section HTML, escaped the way Brevo escapes {{ params.x }}."""
    return html.escape(value, quote=True)


def join_rows(rows):
    """The rows as one block of HTML: a blank line between rows, and no indentation in front of the first
    one (the template's placeholder already stands at that indentation)."""
    return "\n\n".join(rows).lstrip()


def story_row(story, top, bottom):
    """One story: its number badge, headline and paragraphs (P1, P2, P3 - the empty ones left out, the
    others joined by two line breaks, as in the design)."""
    url = esc(clean_url(story["URL"]) or "") if story["URL"] else ""
    description = "<br /><br />".join(rich_text_html(story[p]) for p in ("P1", "P2", "P3") if story[p])
    return STORY_ROW.format(number=story["number"], padding=f"{top} 36px {bottom} 36px", url=url,
                            title=esc(story["TITLE"]), description=description)


def stories_html(data):
    """(stories_before_partner, stories_after_partner): the Top Stories rows above and below the partner banner.

    As in the design: a hairline between two stories, except around the partner banner, which sits between
    stories 2 and 3. The story right under the banner starts 26px below it, and the last story closes the
    section with 32px. With fewer than 3 stories the banner itself is last, so a 32px spacer follows it."""
    stories = entries(data, "STORY")
    above, below = stories[:STORIES_ABOVE_PARTNER], stories[STORIES_ABOVE_PARTNER:]
    before = []
    for index, story in enumerate(above):
        if index:
            before.append(STORY_HAIRLINE_ROW)
        before.append(story_row(story, "24px", "0"))
    after = []
    for index, story in enumerate(below):
        if index:
            after.append(STORY_HAIRLINE_ROW)
        after.append(story_row(story, "26px" if index == 0 else "24px", "32px" if index == len(below) - 1 else "0"))
    return join_rows(before), join_rows(after or [PARTNER_SPACER_ROW])


def trending_rows_html(data):
    """One row per trending coin, in number order: the name, and the 24-hour change in a pill whose text
    colour follows its sign. A hairline under every row but the last, which has the rounded corners."""
    trends = entries(data, "TREND")
    rows = []
    for index, trend in enumerate(trends):
        last = index == len(trends) - 1
        rows.append(TREND_ROW.format(
            number=trend["number"], last_note=" (last row: rounded bottom corners)" if last else "",
            name_edge="border-bottom-left-radius:9px;" if last else TREND_HAIRLINE,
            change_edge="border-bottom-right-radius:9px;" if last else TREND_HAIRLINE,
            name=esc(trend["NAME"]), change=esc(trend["CHANGE"]),
            color=color_value(trend["COLOR"], trend["CHANGE"])))
    return join_rows(rows)


def radar_items_html(data):
    """One bordered panel per radar item, in number order, 12px apart."""
    items = entries(data, "RADAR")
    return join_rows([RADAR_PANEL.format(number=item["number"], label=esc(item["LABEL"]),
                                         text=rich_text_html(item["TEXT"]),
                                         gap=RADAR_GAP_ROW if index < len(items) - 1 else "")
                      for index, item in enumerate(items)])


def build_params(data, first_name, today=None):
    """The Brevo params for ONE email: every template value from newsletter_data.txt,
    plus the recipient's first name. Call check_newsletter_data() first.
    """
    params = {}
    for param, source in PARAM_SOURCES.items():
        value = data.get(source, "")
        if param in COLOR_PARAMS:
            value = color_value(value, data.get(COLOR_PARAMS[param], ""))
        elif param in URL_PARAMS and value:
            value = clean_url(value) or ""
        elif param == "date" and not value:
            value = format_date(today or date.today())
        elif param in DEFAULT_TEXT and not value:
            value = DEFAULT_TEXT[param]
        params[param] = value
    params["stories_before_partner"], params["stories_after_partner"] = stories_html(data)
    params["trending_rows"] = trending_rows_html(data)
    params["radar_items"] = radar_items_html(data)
    params["first_name"] = first_name
    return params


def email_subject(data):
    """The subject line of every email: the first Top Stories heading (STORY_1_TITLE), exactly as typed.
    It is sent together with the template id, so Brevo uses it instead of the template's own subject.
    STORY_1_TITLE is required, so check_newsletter_data() already stops a send when it is empty."""
    return data.get("STORY_1_TITLE", "")


# =============================================================================
# REPORTS (what the scripts print)
# =============================================================================

def param_source(param):
    """Where a param's value comes from, in words (for tables and reports)."""
    if param == "first_name":
        return "Name column of the recipient list"
    if param in COLOR_PARAMS:
        return f"sign of {COLOR_PARAMS[param]} (automatic)"
    if param in SECTION_PARAMS:
        return {"stories_before_partner": "STORY_1, STORY_2",
                "stories_after_partner": "STORY_3, STORY_4 ...",
                "trending_rows": "TREND_1, TREND_2 ...",
                "radar_items": "RADAR_1, RADAR_2 ..."}[param]
    source = PARAM_SOURCES.get(param, "?")
    if param == "date":
        return "DATE (empty = today)"
    if param in DEFAULT_TEXT:
        return f"{source} (empty = design wording)"
    return source


def section_summary(param, data):
    """What a section param holds, in words, e.g. "2 stories (STORY_1 to STORY_2)" or "4 rows (TREND_1 to TREND_4)"."""
    prefix = SECTION_PARAMS[param]
    numbers = entry_numbers(data, prefix)
    if param == "stories_before_partner":
        numbers = numbers[:STORIES_ABOVE_PARTNER]
    elif param == "stories_after_partner":
        numbers = numbers[STORIES_ABOVE_PARTNER:]
        if not numbers:
            return "no story - a 32px space under the partner banner"
    if not numbers:
        return "(none)"
    noun = {"STORY": ("story", "stories"), "TREND": ("row", "rows"), "RADAR": ("item", "items")}[prefix]
    span = f"{prefix}_{numbers[0]}" + (f" to {prefix}_{numbers[-1]}" if len(numbers) > 1 else "")
    return f"{len(numbers)} {noun[len(numbers) != 1]} ({span})"


def params_table(params, data):
    """Text lines showing every param: its name, where the value comes from, and the value."""
    lines = [f"{'Brevo param':<24}{'from newsletter_data.txt':<36}value"]
    for param, value in params.items():
        if param in SECTION_PARAMS:
            shown, note = section_summary(param, data), "   <- safe HTML rows (|safe)"
        else:
            note = "   <- URL" if param in URL_PARAMS else ""
            shown = value if (param in URL_PARAMS or len(value) <= 60) else value[:57] + "..."
        lines.append(f"{param:<24}{param_source(param):<36}{shown or '(empty)'}{note}")
    return lines


def entries_report(data):
    """Text lines listing every numbered entry the email shows, section by section, in the email's order."""
    words = {"up": "green, rising", "down": "red, falling", "flat": "grey, unchanged", None: "UNREADABLE"}
    stories, trends, radar = entries(data, "STORY"), entries(data, "TREND"), entries(data, "RADAR")
    moves = Counter(parse_change(trend["CHANGE"])[0] for trend in trends)
    lines = [f"{SECTION_TITLES['STORY']}: {len(stories)} "
             + (f"(the partner banner comes after story {min(STORIES_ABOVE_PARTNER, len(stories))})" if stories else "")]
    lines += [f"  {story['number']}. {story['TITLE']}" for story in stories]
    lines.append(f"{SECTION_TITLES['TREND']}: {len(trends)} ({moves['up']} rising, {moves['down']} falling, "
                 f"{moves['flat']} unchanged)")
    lines += [f"  {trend['number']}. {trend['NAME']:<36}{trend['CHANGE']:>10}   "
              f"{words[parse_change(trend['CHANGE'])[0]]}" for trend in trends]
    lines.append(f"{SECTION_TITLES['RADAR']}: {len(radar)}")
    lines += [f"  {item['number']}. {item['LABEL']}" for item in radar]
    return lines
