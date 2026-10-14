"""
newsletter_params.py - Build the Brevo "params" from newsletter/newsletter_data.txt.

send_test.py AND send_emails.py both use this file, so the test emails and the
production emails are built in exactly the same way:

    newsletter/newsletter_data.txt          KEY=value lines (prices, stories, URLs ...)
            |  read_newsletter_data()
            v
    check_newsletter_data()                 check_values(): required values present, URLs, colours,
            |                               markup - plus readiness_problems(): no sample content,
            |                               READY_TO_SEND_ON = today (only a TEST may skip these,
            |                               with send_test.py --allow-sample-content)
            v
    build_params(data, first_name)          {"btc_price": ..., "story_1_url": ..., ...}
            |
            v
    Brevo REST API  POST /v3/smtp/email     {"templateId": 1, "params": {...}}

Template 1 (template/brevo_template_1.html) is the designer's original HTML with every
{{UPPERCASE}} placeholder written as {{ params.name }}. The images (logo, hero banner,
partner banner) are fixed in the template exactly as in the original design.

How the values are kept safe
----------------------------
* Plain-text values (titles, prices, names ...) are sent exactly as typed. Brevo
  HTML-escapes every {{ params.x }} itself, so & < > " ' appear exactly as typed and
  cannot break the email's HTML. They must NOT be escaped here as well, or readers
  would see "&amp;" instead of "&".
* Story descriptions (STORY_N_P1..P3) and radar texts (RADAR_N_TEXT) may use **bold**.
  They are turned into a small piece of safe HTML here: the WHOLE text is HTML-escaped
  first, then only **...** becomes <strong>...</strong>, and the paragraphs of a story
  are joined with two line breaks (<br /><br />, as in the design). The template prints
  these values with |safe (RICH_TEXT_PARAMS) - so no other HTML, and never a script,
  can reach the email from the data file.
* Web addresses must start with https:// and are percent-encoded.
"""

import difflib
import html
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from requests.utils import requote_uri

PROJECT_DIR = Path(__file__).resolve().parent
DATA_FILE = PROJECT_DIR / "newsletter" / "newsletter_data.txt"


# =============================================================================
# Brevo template parameter  <-  key(s) in newsletter_data.txt
# (first_name is not in this list: it comes from each recipient's Name.)
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

    # Top stories. The description is built from up to three paragraphs (P2, P3 optional).
    "story_1_title": "STORY_1_TITLE",
    "story_1_description": ("STORY_1_P1", "STORY_1_P2", "STORY_1_P3"),
    "story_1_url": "STORY_1_URL",
    "story_2_title": "STORY_2_TITLE",
    "story_2_description": ("STORY_2_P1", "STORY_2_P2", "STORY_2_P3"),
    "story_2_url": "STORY_2_URL",
    "story_3_title": "STORY_3_TITLE",
    "story_3_description": ("STORY_3_P1", "STORY_3_P2", "STORY_3_P3"),
    "story_3_url": "STORY_3_URL",

    # Trending in crypto (all four rows are shown, as in the design)
    "trend_1_name": "TREND_1_NAME",
    "trend_1_change": "TREND_1_CHANGE",
    "trend_2_name": "TREND_2_NAME",
    "trend_2_change": "TREND_2_CHANGE",
    "trend_3_name": "TREND_3_NAME",
    "trend_3_change": "TREND_3_CHANGE",
    "trend_4_name": "TREND_4_NAME",
    "trend_4_change": "TREND_4_CHANGE",

    # Also on the radar (all three items are shown, as in the design)
    "radar_1_label": "RADAR_1_LABEL",
    "radar_1_text": "RADAR_1_TEXT",
    "radar_2_label": "RADAR_2_LABEL",
    "radar_2_text": "RADAR_2_TEXT",
    "radar_3_label": "RADAR_3_LABEL",
    "radar_3_text": "RADAR_3_TEXT",

    # "Explore More on Coin Edition" links at the bottom of the email
    "xrp_news_url": "XRP_NEWS_URL",
    "india_crypto_news_url": "INDIA_CRYPTO_NEWS_URL",
    "crypto_hacks_url": "CRYPTO_HACKS_URL",
    "crypto_scams_url": "CRYPTO_SCAMS_URL",
    "michael_saylor_url": "MICHAEL_SAYLOR_URL",
    "donald_trump_url": "DONALD_TRUMP_URL",
    "changpeng_zhao_url": "CHANGPENG_ZHAO_URL",
    "vitalik_buterin_url": "VITALIK_BUTERIN_URL",
}


def keys_of(source):
    """The data-file key(s) behind one param: "KEY" -> ("KEY",), a tuple stays a tuple."""
    return source if isinstance(source, tuple) else (source,)


# Every key the data file may contain (besides READY_TO_SEND_ON).
DATA_KEYS = [key for source in PARAM_SOURCES.values() for key in keys_of(source)]

# What the email shows when the title / intro keys are empty: the designer's wording.
DEFAULT_TEXT = {
    "newsletter_title": "DAILY CRYPTO MARKET RECAP",
    "intro_heading": "24-Hour Crypto Recap",
    "intro_description": "Here’s what happened across the crypto market over the past 24 hours.",
}

# Key names used before (and in the designer's first draft of the template). They are still
# accepted and read as the new name, so an older file keeps working.
OLD_KEY_NAMES = {
    "GLOBAL_MARKET_CAP": "GLOBAL_CAP",
    "GLOBAL_MARKET_CHANGE": "GLOBAL_CHANGE",
    "GLOBAL_MARKET_CAP_CHANGE": "GLOBAL_CHANGE",
    **{f"STORY_{n}_DESCRIPTION": f"STORY_{n}_P1" for n in (1, 2, 3)},
    **{f"TRENDING_{n}_{part}": f"TREND_{n}_{part}" for n in (1, 2, 3, 4) for part in ("NAME", "CHANGE")},
    **{f"RADAR_{n}_TITLE": f"RADAR_{n}_LABEL" for n in (1, 2, 3)},
    **{f"RADAR_{n}_DESCRIPTION": f"RADAR_{n}_TEXT" for n in (1, 2, 3)},
}

# Keys of an earlier version that are no longer used: the images are fixed in the template.
RETIRED_KEYS = {"LOGO_IMAGE_URL", "HERO_IMAGE_URL", "PARTNER_IMAGE_URL", "PARTNER_LINK", "PARTNER_ALT",
                "STORY_1_IMAGE_URL", "STORY_2_IMAGE_URL"}

# Not a template value: your confirmation that the content is real and meant for today.
READY_KEY = "READY_TO_SEND_ON"
CONTROL_KEYS = {READY_KEY}

# Params that hold a web address. They are checked by url_problem() and encoded by clean_url().
ARTICLE_URL_PARAMS = ["story_1_url", "story_2_url", "story_3_url"]
URL_PARAMS = ARTICLE_URL_PARAMS + [
    "xrp_news_url", "india_crypto_news_url", "crypto_hacks_url", "crypto_scams_url",
    "michael_saylor_url", "donald_trump_url", "changpeng_zhao_url", "vitalik_buterin_url",
]
# Sites that only serve dummy pages or dummy pictures - never real content.
PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "placehold.co", "placeholder.com",
                     "dummyimage.com", "picsum.photos", "placekitten.com", "lorempixel.com")

# Values that newsletter_params.py turns into safe HTML (escaped text + <strong>), printed
# in the template with |safe. Every other value is plain text that Brevo escapes itself.
RICH_TEXT_PARAMS = [f"story_{n}_description" for n in (1, 2, 3)] + [f"radar_{n}_text" for n in (1, 2, 3)]
RICH_TEXT_KEYS = [f"STORY_{n}_P{p}" for n in (1, 2, 3) for p in (1, 2, 3)] + [f"RADAR_{n}_TEXT" for n in (1, 2, 3)]

# Text colour of each 24-hour change ({{ params.btc_color }} etc.) <- the change it follows.
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
TREND_CHANGE_KEYS = [f"TREND_{n}_CHANGE" for n in (1, 2, 3, 4)]
PRICE_KEYS = ["BTC_PRICE", "ETH_PRICE", "BNB_PRICE", "XRP_PRICE", "SOL_PRICE", "GLOBAL_CAP"]

# Keys that may be left empty. Everything else is shown in the design, so it is required.
OPTIONAL_KEYS = {
    "DATE", "NEWSLETTER_TITLE", "INTRO_HEADING", "INTRO_DESCRIPTION",
    "BTC_COLOR", "ETH_COLOR", "BNB_COLOR", "XRP_COLOR", "SOL_COLOR", "GLOBAL_COLOR",
    "STORY_1_P2", "STORY_1_P3", "STORY_2_P2", "STORY_2_P3", "STORY_3_P2", "STORY_3_P3",
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


def read_newsletter_data(path=DATA_FILE):
    """Read the KEY=value lines of newsletter_data.txt. Returns (data, problems).

    Everything after the first "=" is the value, so URLs containing "=" are fine.
    Lines starting with # and blank lines are ignored. Keys are not case-sensitive,
    and the old key names in OLD_KEY_NAMES are read as their new names.
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
        key = OLD_KEY_NAMES.get(written, written)
        if not key:
            problems.append(f"line {number} has no key name before the '='")
            continue
        if key in data:
            also = f" (also written as {written_as[key]})" if written_as[key] != written else ""
            problems.append(f"line {number}: {key} is in the file twice{also} - keep only one")
        written_as[key] = written
        data[key] = clean_text(value)
    return data, problems


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
    """Every check that does not depend on the day: required values present, web addresses, colours,
    markup, prices and changes. Returns (problems, warnings). Applies to test AND production emails."""
    problems = []
    warnings = []

    for key in DATA_KEYS:
        if key in OPTIONAL_KEYS:
            continue
        if key not in data:
            problems.append(f"{key} is required, but its line is missing from the file")
        elif not data[key]:
            problems.append(f"{key} is required, but it is empty")

    for param in URL_PARAMS:
        key = PARAM_SOURCES[param]
        value = data.get(key, "")
        problem = url_problem(value) if value else None
        if problem:
            problems.append(f"{key} {problem}: {value}")

    url_keys = {PARAM_SOURCES[p] for p in URL_PARAMS}
    color_keys = {PARAM_SOURCES[p] for p in COLOR_PARAMS}
    for key in DATA_KEYS:
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
        elif key in RICH_TEXT_KEYS and rich_text_problem(value):
            problems.append(f"{key} {rich_text_problem(value)}")
        elif key not in RICH_TEXT_KEYS and "**" in value:
            problems.append(f"{key} contains ** - bold is only possible in story paragraphs and radar texts")

    for key in PRICE_KEYS:
        value = data.get(key, "")
        if value and not re.search(r"\d", value):
            problems.append(f"{key}={value} has no number in it - write the figure, e.g. $84,612.30")

    for param, change_key in COLOR_PARAMS.items():
        change = data.get(change_key, "")
        if not change:
            continue  # reported above as a missing required value
        direction, reason = parse_change(change)
        if reason:
            problems.append(f"{change_key}={change} {reason}")
            continue
        color_key = PARAM_SOURCES[param]
        color = data.get(color_key, "").strip().lower()
        if color and color not in COLOR_WORDS:
            problems.append(f"{color_key}={data[color_key]} is not allowed - leave it empty (recommended) so the "
                            f"colour follows the sign of {change_key}, or write green, red or grey")
        elif color and COLOR_WORDS[color] != direction:
            problems.append(f"{color_key}={data[color_key]} disagrees with {change_key}={change} - leave "
                            f"{color_key} empty so the colour always matches the change")

    for key in TREND_CHANGE_KEYS:
        change = data.get(key, "")
        if not change:
            continue
        direction, reason = parse_change(change)
        if reason:
            problems.append(f"{key}={change} {reason}")
        elif direction != "up":
            problems.append(f"{key}={change} is not a rise - 'Trending in Crypto' lists the strongest movers "
                            "and shows every change in green, so only rises like +9.8% can be shown")

    for key, value in data.items():
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            warnings.append(f"{key} is wrapped in quotes - the quotes will appear in the email")

    for key in data:
        if key in RETIRED_KEYS:
            warnings.append(f"{key} is no longer used - the images are fixed in the template, as in the "
                            "original design (the line can be deleted)")
        elif key not in DATA_KEYS and key not in CONTROL_KEYS:
            guess = difflib.get_close_matches(key, DATA_KEYS + list(CONTROL_KEYS), n=1, cutoff=0.8)
            hint = f" - did you mean {guess[0]}?" if guess else " (a typo?)"
            warnings.append(f"{key} is not used by the template{hint}")
    return problems, warnings


def format_date(day):
    """A date as newsletter text, e.g. 'Thursday, October 8, 2026'."""
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"


def build_params(data, first_name, today=None):
    """The Brevo params for ONE email: every template value from newsletter_data.txt,
    plus the recipient's first name. Call check_newsletter_data() first.
    """
    params = {}
    for param, source in PARAM_SOURCES.items():
        if isinstance(source, tuple):  # a story description: its paragraphs, as safe HTML
            paragraphs = [data.get(key, "") for key in source]
            value = "<br /><br />".join(rich_text_html(p) for p in paragraphs if p)
        else:
            value = data.get(source, "")
            if param in COLOR_PARAMS:
                value = color_value(value, data.get(COLOR_PARAMS[param], ""))
            elif param in URL_PARAMS and value:
                value = clean_url(value) or ""
            elif param in RICH_TEXT_PARAMS and value:
                value = rich_text_html(value)
            elif param == "date" and not value:
                value = format_date(today or date.today())
            elif param in DEFAULT_TEXT and not value:
                value = DEFAULT_TEXT[param]
        params[param] = value
    params["first_name"] = first_name
    return params


def param_source(param):
    """Where a param's value comes from, in words (for tables and reports)."""
    if param == "first_name":
        return "Name column of the recipient list"
    if param in COLOR_PARAMS:
        return f"sign of {COLOR_PARAMS[param]} (automatic)"
    source = PARAM_SOURCES.get(param, "?")
    if isinstance(source, tuple):
        return f"{source[0]} (+ {source[1][-2:]}, {source[2][-2:]} optional)"
    if param == "date":
        return "DATE (empty = today)"
    if param in DEFAULT_TEXT:
        return f"{source} (empty = design wording)"
    return source


def params_table(params, data):
    """Text lines showing every param: its name, where the value comes from, and the value."""
    lines = [f"{'Brevo param':<24}{'from newsletter_data.txt':<36}value"]
    for param, value in params.items():
        if param in ARTICLE_URL_PARAMS:
            note = "   <- article URL"
        elif param in URL_PARAMS:
            note = "   <- URL"
        elif param in RICH_TEXT_PARAMS and value:
            note = "   <- safe HTML (|safe)"
        else:
            note = ""
        shown = value if (param in URL_PARAMS or len(value) <= 60) else value[:57] + "..."
        lines.append(f"{param:<24}{param_source(param):<36}{shown or '(empty)'}{note}")
    return lines
