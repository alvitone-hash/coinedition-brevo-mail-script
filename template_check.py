"""
template_check.py - Check a Brevo email template against the params the scripts send,
and draw a local preview of the email. Used by send_test.py, send_emails.py,
template_sync.py and the tests, so the same rules apply everywhere.

check_template(html, params) - rules for the template text:
  * every {{ params.x }} must be a value the scripts send, with the exact (case-sensitive) name
  * every value the scripts send must appear somewhere in the template
  * |safe only on the values newsletter_params.py turns into safe HTML (RICH_TEXT_PARAMS),
    and always on those
  * only {% if params.x %} / {% else %} / {% endif %}, properly closed
  * no tags in another format such as {{BTC_PRICE}} (Brevo would leave them blank)
  * no tags inside <!-- comments --> (Brevo fills them in even there)
  * an Unsubscribe link: href="{{ unsubscribe }}"
  * no leftover YOUR_... / ?REPLACE= markers

render(html, params) + check_rendered(): the email as Brevo would build it for these tags
(every value HTML-escaped except |safe ones), then checked for empty/"#"/non-https links,
empty image addresses, unbalanced tags and leftover template tags.
The preview is a local approximation - only a real test email shows exactly what an
inbox displays.

Check a LOCAL template file before pasting it into Brevo (offline, sends nothing):
    python template_check.py                        checks template/brevo_template_1.html
    python template_check.py path/to/other.html
It prints which data-file key fills every placeholder and saves logs/template_preview.html.
"""

import html as html_lib
import re
from html.parser import HTMLParser

from newsletter_params import RICH_TEXT_PARAMS, URL_PARAMS

TAG = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
VARIABLE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?:\|\s*([A-Za-z_]+)\s*)?$")
IF_PARAM = re.compile(r"^if\s+params\.([A-Za-z0-9_]+)$")
COMMENT = re.compile(r"<!--.*?-->", re.S)
BREVO_BUILT_INS = {"unsubscribe", "mirror", "update_profile"}
UNSUBSCRIBE_LINK = re.compile(r'href\s*=\s*"\{\{\s*unsubscribe\s*\}\}"')
LEFTOVER_MARKERS = re.compile(r"YOUR_[A-Z0-9_]+|\?REPLACE=|goes here later", re.IGNORECASE)
PREVIEW_UNSUBSCRIBE = "https://unsubscribe.preview.invalid/brevo-fills-this-in"


class TemplateSyntaxError(Exception):
    """The template uses a tag the preview can't draw."""


def _line(text, index):
    return text.count("\n", 0, index) + 1


def placeholders(html):
    """Every {{ params.x }} / {% if params.x %} name used in the template, in order of first use."""
    names = []
    for match in TAG.finditer(html):
        if match.group(1) is not None:
            variable = VARIABLE.match(match.group(1))
            name = variable.group(1) if variable else ""
        else:
            condition = IF_PARAM.match(match.group(2).strip())
            name = "params." + condition.group(1) if condition else ""
        if name.startswith("params.") and name[7:] not in names:
            names.append(name[7:])
    return names


def check_template(html, params):
    """Check the template text. Returns (problems, warnings) - problems mean it must not be sent."""
    problems, warnings = [], []
    comments = [(m.start(), m.end()) for m in COMMENT.finditer(html)]
    used = set()
    open_blocks = []  # [line, has_else]

    for match in TAG.finditer(html):
        tag, line = match.group(0), _line(html, match.start())
        if any(start <= match.start() < end for start, end in comments):
            problems.append(f"line {line}: {tag} is inside an HTML comment - Brevo fills tags in even there; "
                            "remove it from the comment")
        if match.group(1) is not None:                                  # {{ ... }}
            variable = VARIABLE.match(match.group(1))
            if not variable:
                problems.append(f"line {line}: {tag} is not a tag this project can check")
                continue
            name, filter_name = variable.groups()
            if name in BREVO_BUILT_INS:
                if filter_name:
                    problems.append(f"line {line}: {tag} - Brevo's own links take no filter")
                continue
            if name.startswith("contact."):
                warnings.append(f"line {line}: {tag} is filled from Brevo Contacts, not by these scripts")
                continue
            if not name.startswith("params."):
                problems.append(f"line {line}: {tag} is not in Brevo's params format, so Brevo would leave it "
                                "blank - it must be written {{ params.name }} with a name the scripts send")
                continue
            param = name[len("params."):]
            used.add(param)
            if param not in params:
                problems.append(f"line {line}: {tag} - the scripts send no value called '{param}' "
                                "(names are case-sensitive), so it would be blank")
            if filter_name not in (None, "safe"):
                problems.append(f"line {line}: {tag} - the |{filter_name} filter is not used by this project")
            elif filter_name == "safe" and param not in RICH_TEXT_PARAMS:
                problems.append(f"line {line}: {tag} - |safe is only allowed on the story paragraphs and radar "
                                "texts, which the script turns into safe HTML; this value must be escaped by Brevo")
            elif filter_name is None and param in RICH_TEXT_PARAMS:
                problems.append(f"line {line}: {tag} needs |safe - the script sends it as ready-made safe HTML "
                                "(without |safe readers would see &lt;strong&gt; codes)")
        else:                                                            # {% ... %}
            content = re.sub(r"\s+", " ", match.group(2).strip())
            condition = IF_PARAM.match(content)
            if condition:
                param = condition.group(1)
                used.add(param)
                if param not in params:
                    problems.append(f"line {line}: {tag} - the scripts send no value called '{param}'")
                open_blocks.append([line, False])
            elif content == "else":
                if not open_blocks or open_blocks[-1][1]:
                    problems.append(f"line {line}: {{% else %}} without a matching {{% if %}}")
                else:
                    open_blocks[-1][1] = True
            elif content == "endif":
                if not open_blocks:
                    problems.append(f"line {line}: {{% endif %}} without a matching {{% if %}}")
                else:
                    open_blocks.pop()
            else:
                problems.append(f"line {line}: {tag} - only if/else/endif on params are used in this template")
    for line, _ in open_blocks:
        problems.append(f"line {line}: an {{% if %}} block is never closed with {{% endif %}}")

    unused = [p for p in params if p not in used]
    if unused:
        problems.append("these values are sent but the template never shows them (a missing placeholder?): "
                        + ", ".join(unused))
    if not UNSUBSCRIBE_LINK.search(html):
        problems.append('the template has no working Unsubscribe link - it needs <a href="{{ unsubscribe }}">')
    outside_comments = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), html)
    for marker in LEFTOVER_MARKERS.finditer(outside_comments):
        problems.append(f"line {_line(outside_comments, marker.start())}: placeholder text "
                        f"'{marker.group()}' is still in the template")
    return problems, warnings


def render(html, params, unsubscribe=PREVIEW_UNSUBSCRIBE):
    """Build the email the way Brevo does for the tags used here (local preview only)."""
    output, position, stack = [], 0, []  # stack of [condition is true, parent is showing]

    def showing():
        return all(cond and parent for cond, parent in stack)

    for match in TAG.finditer(html):
        if showing():
            output.append(html[position:match.start()])
        position = match.end()
        if match.group(1) is not None:
            variable = VARIABLE.match(match.group(1))
            if not variable:
                raise TemplateSyntaxError(f"line {_line(html, match.start())}: {match.group(0)}")
            if not showing():
                continue
            name, filter_name = variable.groups()
            if name == "unsubscribe":
                output.append(unsubscribe)
            elif name.startswith("params."):
                value = str(params.get(name[7:], ""))
                output.append(value if filter_name == "safe" else html_lib.escape(value, quote=True))
            # anything else is blank, as in Brevo
        else:
            content = re.sub(r"\s+", " ", match.group(2).strip())
            condition = IF_PARAM.match(content)
            if condition:
                stack.append([bool(params.get(condition.group(1))), showing()])
            elif content == "else" and stack:
                stack[-1][0] = not stack[-1][0]
            elif content == "endif" and stack:
                stack.pop()
            else:
                raise TemplateSyntaxError(f"line {_line(html, match.start())}: {match.group(0)}")
    if showing():
        output.append(html[position:])
    if stack:
        raise TemplateSyntaxError("an {% if %} block is never closed")
    return "".join(output)


class _Collector(HTMLParser):
    CHECKED = ("a", "div", "table", "tr", "td", "strong", "span")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.images, self.depth, self.errors = [], [], {t: 0 for t in self.CHECKED}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self.links.append((a.get("href"), self.getpos()[0]))
        if tag == "img":
            self.images.append((a.get("src"), self.getpos()[0]))
        if tag in self.depth:
            self.depth[tag] += 1

    def handle_endtag(self, tag):
        if tag in self.depth:
            self.depth[tag] -= 1
            if self.depth[tag] < 0:
                self.errors.append(f"line {self.getpos()[0]}: </{tag}> without an opening <{tag}>")
                self.depth[tag] = 0


def check_rendered(email_html):
    """Problems in a rendered email: broken links/images, leftover tags, unbalanced HTML."""
    problems = []
    without_comments = COMMENT.sub("", email_html)
    if "{{" in without_comments or "{%" in without_comments:
        problems.append("template tags are left in the email")
    collector = _Collector()
    collector.feed(email_html)
    for href, line in collector.links:
        target = (href or "").strip()
        if target in ("", "#"):
            problems.append(f"line {line}: a link goes nowhere (href=\"{target}\")")
        elif not (target.startswith("https://") or target.startswith("mailto:")):
            problems.append(f"line {line}: link {target[:60]} is not https://")
    for src, line in collector.images:
        if not (src or "").strip():
            problems.append(f"line {line}: an image has no address (it would show as broken)")
        elif not src.startswith("https://"):
            problems.append(f"line {line}: image {src[:60]} is not https://")
    problems += collector.errors
    problems += [f"<{tag}> opened {count} more time(s) than closed" for tag, count in collector.depth.items() if count]
    return problems


def link_report(email_html):
    """(href, line) of every link and (src, line) of every image in a rendered email."""
    collector = _Collector()
    collector.feed(email_html)
    return collector.links, collector.images


def check_url_params(params):
    """Problems with the web addresses in the params themselves (all must be https or empty)."""
    return [f"params.{name} = {params[name]!r} is not an https:// address"
            for name in URL_PARAMS if params.get(name) and not str(params[name]).startswith("https://")]


# =============================================================================
# python template_check.py [file] - check a LOCAL template before pasting it into Brevo
# =============================================================================

def main(argv=None):
    import argparse
    import sys
    from pathlib import Path

    from newsletter_params import (OPTIONAL_KEYS, build_params, check_newsletter_data, param_source,
                                   read_newsletter_data)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Check a local template file. Offline - sends nothing.")
    parser.add_argument("file", nargs="?", default=str(project / "template" / "brevo_template_1.html"))
    args = parser.parse_args(argv)
    path = Path(args.file)
    html = path.read_bytes().decode("utf-8")

    data, read_problems = read_newsletter_data()
    params = build_params(data, "Subscriber")
    problems, warnings = check_template(html, params)
    email = render(html, params)
    problems += [f"preview with today's values: {p}" for p in check_rendered(email)]
    # The same email when every OPTIONAL value is empty - nothing may break or show a broken image.
    bare = dict(data, **{key: "" for key in OPTIONAL_KEYS})
    problems += [f"preview with optional values empty: {p}"
                 for p in check_rendered(render(html, build_params(bare, "")))]
    preview = project / "logs" / "template_preview.html"
    preview.parent.mkdir(exist_ok=True)
    preview.write_text(email, encoding="utf-8")

    print(f"Template file: {path} ({len(html):,} characters)")
    print(f"\n{'placeholder in the template':<38}{'filled from (newsletter_data.txt)':<44}value now")
    for name in placeholders(html):
        value = str(params.get(name, ""))
        tag = "{{ params." + name + ("|safe" if name in RICH_TEXT_PARAMS else "") + " }}"
        print(f"{tag:<38}{param_source(name):<44}{(value[:45] + '...') if len(value) > 45 else value or '(empty)'}")
    links, images = link_report(email)
    print(f"\nPreview: {len(links)} links, {len(images)} images -> {preview}")
    print(f"\nTEMPLATE CHECK: {len(problems)} problem(s), {len(warnings)} warning(s)")
    for line in problems + warnings:
        print(f"  - {line}")
    content_problems, _ = check_newsletter_data(data)
    content_problems = read_problems + content_problems
    print(f"\nCONTENT CHECK (newsletter/newsletter_data.txt): {len(content_problems)} value(s) still to fix before sending")
    for line in content_problems:
        print(f"  - {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
