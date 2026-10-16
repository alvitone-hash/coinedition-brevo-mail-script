"""Top Stories, Trending and Radar: exactly the entries in the data file, in order, with the design's rows.

The number of entries comes from the data file (STORY_N_..., TREND_N_..., RADAR_N_...). These tests try
1 to 8 entries per section, the checks on numbering, signs and markup, and that the email still looks
exactly as the design that was live in Brevo on 2026-10-14.
"""

import hashlib
import html
import re
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import support  # noqa: F401  (switches off the network, sets the import path)
from support import MASTER_TEMPLATE, good_data

import newsletter_params as np
import template_check as tc

TODAY = date(2026, 10, 14)
MASTER = MASTER_TEMPLATE.read_text(encoding="utf-8")

# sha256 of the email built from good_data(2026-10-14) - every tag with all its attributes and inline styles
# and every piece of text, in order (see canonical(); comments and whitespace ignored) - as the template and
# code that were live in Brevo on 2026-10-14 (3 stories, 5 trending rows, 3 radar items, fixed in the
# template) built it, before the sections followed the data file.
DESIGN_OF_2026_10_14 = "cc2fce62fdf70f4b7b16d211bf3b89228f75b248dec02ecaccbcb2518a37e147"

PILL = re.compile(r'<td class="pill" style="[^"]*color:(#[0-9A-F]{6});[^"]*">\s*(.*?)\s*</td>', re.S)
TREND_NAME = re.compile(r'<td class="cell-sm" style="padding:13px 16px; ([^"]*?)font-family[^"]*">\s*(.*?)\s*</td>\s*'
                        r'<td align="right" class="cell-sm" style="padding:13px 16px; ([^"]*)">', re.S)
BADGE = re.compile(r'line-height:28px; font-weight:bold; color:#181C47; mso-line-height-rule:exactly;">\s*(\d+)\s*</td>')
STORY_ROW = re.compile(r'<td class="px" style="padding:(\d+px) 36px (\S+) 36px;">\s*<table[^>]*>\s*<tr>\s*'
                       r'(?:<!--.*?-->\s*)?<td width="42"')
TITLE = re.compile(r'<div class="h3" data-story-url="([^"]*)"[^>]*>\s*(.*?)\s*</div>', re.S)
RADAR_LABEL = re.compile(r'<td style="padding:16px 18px; border:1px solid #E5E5E5; border-radius:10px;">\s*'
                         r'<div[^>]*>\s*(.*?)\s*</div>', re.S)
RADAR_GAP = '<tr><td height="12" style="height:12px; line-height:12px; font-size:0;">&nbsp;</td></tr>'
PARTNER = '<img src="https://coinedition.com/wp-content/uploads/2026/10/unchained-summit-india02.webp"'
SPACER = '<tr><td height="32" style="height:32px; line-height:32px; font-size:0;">&nbsp;</td></tr>'


def with_entries(stories=3, trends=5, radar=3, **changes):
    """good_data with exactly this many entries per section, then the changes."""
    data = {key: value for key, value in good_data(TODAY).items() if not np.entry_key_parts(key)}
    for n in range(1, stories + 1):
        data.update({f"STORY_{n}_TITLE": f"Story {n} headline", f"STORY_{n}_P1": f"Story {n} **first** paragraph.",
                     f"STORY_{n}_URL": f"https://coinedition.com/story-{n}/"})
    for n in range(1, trends + 1):
        data.update({f"TREND_{n}_NAME": f"Coin {n} (C{n})", f"TREND_{n}_CHANGE": ("+" if n % 2 else "-") + f"{n}.5%"})
    for n in range(1, radar + 1):
        data.update({f"RADAR_{n}_LABEL": f"Radar {n}", f"RADAR_{n}_TEXT": f"Radar {n} text with **bold**."})
    data.update(changes)
    return data


def email_for(data):
    params = np.build_params(data, "Subscriber", TODAY)
    return params, tc.render(MASTER, params)


def problems_of(data):
    return np.check_values(data)[0]


class _Tokens(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.tokens, self._text = [], []
        self.feed(page)
        self.close()
        self._flush()

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._text))
        if text.strip():
            self.tokens.append(("text", text))
        self._text = []

    def handle_starttag(self, tag, attrs):
        self._flush()
        self.tokens.append(("start", tag, tuple(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.tokens.append(("end", tag))

    def handle_endtag(self, tag):
        self._flush()
        self.tokens.append(("end", tag))

    def handle_data(self, data):
        self._text.append(data)


def canonical(page):
    """The email as a document: every tag with all its attributes, every end tag and every piece of text, in
    order. HTML comments are ignored and runs of whitespace count as one space."""
    return repr(_Tokens(page).tokens)


def visible_text(page):
    return " ".join(text for kind, *rest in _Tokens(page).tokens if kind == "text" for text in rest)


class DesignTests(unittest.TestCase):
    def test_three_stories_five_rows_three_items_look_exactly_as_on_2026_10_14(self):
        _, page = email_for(good_data(TODAY))
        self.assertEqual(hashlib.sha256(canonical(page).encode("utf-8")).hexdigest(), DESIGN_OF_2026_10_14)

    def test_the_template_has_one_placeholder_per_section_and_no_logic(self):
        self.assertEqual(set(tc.placeholders(MASTER)), set(np.build_params(good_data(TODAY), "x", TODAY)))
        for param in np.SAFE_HTML_PARAMS:
            self.assertEqual(MASTER.count("{{ params." + param + "|safe }}"), 1, param)
        self.assertEqual(set(re.findall(r"\{\{ params\.([a-z0-9_]+)\|safe \}\}", MASTER)), set(np.SAFE_HTML_PARAMS))
        self.assertNotIn("{%", MASTER)
        self.assertEqual(re.findall(r"params\.(?:story|trend|radar)_\d", MASTER), [])   # no fixed rows left

    def test_any_number_of_entries_gives_a_clean_email(self):
        for shape in [(1, 1, 1), (2, 4, 2), (3, 4, 3), (4, 6, 4), (6, 8, 5)]:
            with self.subTest(shape=shape):
                data = with_entries(*shape)
                params, page = email_for(data)
                self.assertEqual(np.check_values(data), ([], []))
                self.assertEqual(tc.check_template(MASTER, params), ([], []))
                self.assertEqual(tc.check_rendered(page), [])


class TrendingTests(unittest.TestCase):
    def test_exactly_the_rows_in_the_file_in_their_order(self):
        for count in range(1, 9):
            with self.subTest(rows=count):
                _, page = email_for(with_entries(trends=count))
                self.assertEqual([name for _, name, _ in TREND_NAME.findall(page)],
                                 [f"Coin {n} (C{n})" for n in range(1, count + 1)])
                self.assertEqual([change for _, change in PILL.findall(page)],
                                 [("+" if n % 2 else "-") + f"{n}.5%" for n in range(1, count + 1)])

    def test_hairline_under_every_row_but_the_last_which_has_the_rounded_corners(self):
        for count in (1, 4, 6):
            with self.subTest(rows=count):
                _, page = email_for(with_entries(trends=count))
                edges = [(name_edge.strip(), change_edge.strip()) for name_edge, _, change_edge in TREND_NAME.findall(page)]
                self.assertEqual(edges[:-1], [("border-bottom:1px solid #EEEEEE;",) * 2] * (count - 1))
                self.assertEqual(edges[-1], ("border-bottom-left-radius:9px;", "border-bottom-right-radius:9px;"))

    def test_rises_and_falls_are_both_accepted_and_coloured_by_their_sign(self):
        data = with_entries(trends=6, TREND_1_CHANGE="-12.05%", TREND_2_CHANGE="+5.12%", TREND_3_CHANGE="0.00%",
                            TREND_4_CHANGE="−2.42%", TREND_5_CHANGE="+0%", TREND_6_CHANGE="-99.55%")
        self.assertEqual(problems_of(data), [])
        _, page = email_for(data)
        self.assertEqual(PILL.findall(page), [(np.FALL_COLOR, "-12.05%"), (np.RISE_COLOR, "+5.12%"),
                                              (np.FLAT_COLOR, "0.00%"), (np.FALL_COLOR, "−2.42%"),
                                              (np.FLAT_COLOR, "+0%"), (np.FALL_COLOR, "-99.55%")])

    def test_every_row_needs_a_sign_and_a_matching_colour(self):
        self.assertIn("TREND_6_CHANGE=3.0% needs a + or - sign, e.g. +0.35% or -0.85% (only 0% may be written "
                      "without one)", problems_of(with_entries(trends=6, TREND_6_CHANGE="3.0%")))
        self.assertTrue(any("not a percentage" in p for p in problems_of(with_entries(trends=7, TREND_7_CHANGE="up"))))
        self.assertIn("TREND_6_COLOR=green disagrees with TREND_6_CHANGE=-6.5% - leave TREND_6_COLOR empty so the "
                      "colour always matches the change", problems_of(with_entries(trends=6, TREND_6_COLOR="green")))
        self.assertEqual(problems_of(with_entries(trends=6, TREND_6_COLOR="red")), [])


class StoryTests(unittest.TestCase):
    def test_exactly_the_stories_in_the_file_numbered_in_order(self):
        for count in range(1, 7):
            with self.subTest(stories=count):
                _, page = email_for(with_entries(stories=count))
                self.assertEqual(BADGE.findall(page), [str(n) for n in range(1, count + 1)])
                self.assertEqual([title for _, title in TITLE.findall(page)],
                                 [f"Story {n} headline" for n in range(1, count + 1)])

    def test_the_partner_banner_stays_between_stories_2_and_3(self):
        for count in range(1, 7):
            with self.subTest(stories=count):
                _, page = email_for(with_entries(stories=count))
                banner = page.index(PARTNER)
                titles = [page.index(f"Story {n} headline") for n in range(1, count + 1)]
                self.assertEqual(sum(1 for t in titles if t < banner), min(2, count))
                self.assertEqual(page.count(PARTNER), 1)

    def test_spacing_as_in_the_design(self):
        # 3 stories: 24px, 24px (after a hairline), then 26px under the banner with the section's 32px at the end
        _, page = email_for(with_entries(stories=3))
        self.assertEqual(STORY_ROW.findall(page), [("24px", "0"), ("24px", "0"), ("26px", "32px")])
        self.assertEqual(page.count("<!-- hairline between stories -->"), 1)
        self.assertNotIn(SPACER, page)
        # 5 stories: hairlines between 1-2, 3-4 and 4-5; the last one closes the section
        _, page = email_for(with_entries(stories=5))
        self.assertEqual(STORY_ROW.findall(page),
                         [("24px", "0"), ("24px", "0"), ("26px", "0"), ("24px", "0"), ("24px", "32px")])
        self.assertEqual(page.count("<!-- hairline between stories -->"), 3)
        # 1 or 2 stories: the banner is last, followed by the same 32px
        for count in (1, 2):
            _, page = email_for(with_entries(stories=count))
            self.assertEqual(STORY_ROW.findall(page), [("24px", "0")] * count)
            self.assertEqual(page.count(SPACER), 1)
            self.assertLess(page.index(PARTNER), page.index(SPACER))

    def test_paragraphs_are_joined_and_the_empty_ones_left_out(self):
        _, page = email_for(with_entries(stories=4, STORY_4_P1="A", STORY_4_P2="", STORY_4_P3="**C** & d"))
        self.assertIn("A<br /><br /><strong>C</strong> &amp; d", page)
        self.assertNotIn("<br /><br />\n", page)

    def test_the_subject_is_still_the_first_story_heading(self):
        self.assertEqual(np.email_subject(with_entries(stories=5)), "Story 1 headline")


class RadarTests(unittest.TestCase):
    def test_exactly_the_items_in_the_file_12px_apart(self):
        for count in range(1, 6):
            with self.subTest(items=count):
                _, page = email_for(with_entries(radar=count))
                self.assertEqual(RADAR_LABEL.findall(page), [f"Radar {n}" for n in range(1, count + 1)])
                self.assertEqual(page.count(RADAR_GAP), count - 1)
                self.assertEqual(page.count("text with <strong>bold</strong>."), count)


class NumberingTests(unittest.TestCase):
    def test_a_section_needs_at_least_one_entry(self):
        for prefix, message in [("STORY", "Top Stories has no entries - add at least STORY_1_TITLE and STORY_1_P1"),
                                ("TREND", "Trending Searches & Gainers has no entries - add at least TREND_1_NAME "
                                          "and TREND_1_CHANGE"),
                                ("RADAR", "Also on the Radar has no entries - add at least RADAR_1_LABEL and "
                                          "RADAR_1_TEXT")]:
            with self.subTest(prefix=prefix):
                data = {k: v for k, v in with_entries().items() if not k.startswith(prefix)}
                self.assertIn(message, problems_of(data))

    def test_a_gap_is_refused_and_nothing_is_renumbered(self):
        data = {k: v for k, v in with_entries(trends=5).items() if not k.startswith("TREND_3_")}
        self.assertIn("Trending Searches & Gainers: the entries must be numbered 1, 2, 3 ... without gaps, but "
                      "TREND_3 is missing (the file has TREND_1 to TREND_2, TREND_4 to TREND_5) - renumber them in "
                      "the file; nothing is renumbered or left out automatically", problems_of(data))
        data = {k: v for k, v in with_entries(radar=2).items() if not k.startswith("RADAR_1_")}
        self.assertTrue(any("RADAR_1 is missing" in p for p in problems_of(data)))

    def test_every_entry_needs_its_required_values(self):
        self.assertIn("TREND_5_CHANGE is required, but its line is missing from the file",
                      problems_of(dict(with_entries(trends=4), TREND_5_NAME="Coin 5")))
        self.assertIn("STORY_2_P1 is required, but it is empty", problems_of(with_entries(STORY_2_P1="")))
        self.assertIn("RADAR_3_TEXT is required, but it is empty", problems_of(with_entries(RADAR_3_TEXT="")))
        self.assertIn("STORY_4 has lines in the file but no values - fill in STORY_4_TITLE and STORY_4_P1, or "
                      "delete the STORY_4 lines if Top Stories has only 3 today",
                      problems_of(dict(with_entries(stories=3), STORY_4_P3="", STORY_4_URL="")))

    def test_optional_fields_may_be_empty_for_any_entry(self):
        data = with_entries(stories=5, trends=7, STORY_5_P2="", STORY_5_P3="", STORY_5_URL="", TREND_7_COLOR="")
        self.assertEqual(np.check_values(data), ([], []))
        self.assertTrue({"STORY_5_P2", "STORY_5_URL", "TREND_7_COLOR"} <= np.optional_keys(data))

    def test_a_mistyped_entry_line_blocks_instead_of_silently_disappearing(self):
        problems = problems_of(with_entries(TREND_01_NAME="Coin"))
        self.assertTrue(any(p.startswith("TREND_01_NAME is not a key the newsletter knows - did you mean "
                                         "TREND_1_NAME?") for p in problems), problems)
        self.assertTrue(any("STORY_2_HEADLINE is not a key" in p for p in problems_of(with_entries(STORY_2_HEADLINE="x"))))
        problems, warnings = np.check_values(with_entries(STORY_1_IMAGE_URL="https://coinedition.com/x.webp"))
        self.assertEqual(problems, [])   # a retired key is still only a note
        self.assertTrue(any("images are fixed in the template" in w for w in warnings))

    def test_old_names_are_read_for_any_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.txt"
            path.write_text("TRENDING_6_NAME=Sui\nTRENDING_6_CHANGE=+1%\nSTORY_4_DESCRIPTION=Text\n"
                            "RADAR_5_TITLE=Label\nRADAR_5_DESCRIPTION=More\n", encoding="utf-8")
            data, problems = np.read_newsletter_data(path)
        self.assertEqual(problems, [])
        self.assertEqual(data, {"TREND_6_NAME": "Sui", "TREND_6_CHANGE": "+1%", "STORY_4_P1": "Text",
                                "RADAR_5_LABEL": "Label", "RADAR_5_TEXT": "More"})


class RecapLineTests(unittest.TestCase):
    """The line under Explore More: "Market figures and developments reflect the <date> Coin Edition recap. Read the
    full report" - its date and link come from RECAP_DATE and RECAP_URL in the data file."""
    LINE = re.compile(r'reflect the (.*?) Coin Edition recap\. <a href="([^"]*)"[^>]*>Read the full report</a>')
    RECAP_75 = "https://coinedition.com/24-hour-crypto-recap-heres-what-happened-in-the-market-75/"

    def test_the_recap_date_and_link_come_from_the_data_file(self):
        _, page = email_for(with_entries(RECAP_DATE="October 15, 2026", RECAP_URL=self.RECAP_75))
        self.assertEqual(self.LINE.findall(page), [("October 15, 2026", self.RECAP_75)])

    def test_both_are_required_and_the_link_must_be_a_real_https_page(self):
        self.assertIn("RECAP_DATE is required, but it is empty", problems_of(with_entries(RECAP_DATE="")))
        data = with_entries()
        del data["RECAP_URL"]
        self.assertIn("RECAP_URL is required, but its line is missing from the file", problems_of(data))
        self.assertTrue(any(p.startswith("RECAP_URL must start with https://")
                            for p in problems_of(with_entries(RECAP_URL="http://coinedition.com/x"))))
        self.assertTrue(any(p.startswith("RECAP_URL is a placeholder address")
                            for p in problems_of(with_entries(RECAP_URL="https://example.com/recap"))))
        self.assertTrue(any(p.startswith("RECAP_DATE=soon has no number in it")
                            for p in problems_of(with_entries(RECAP_DATE="soon"))))

    def test_a_recap_date_other_than_the_issue_date_is_pointed_out(self):
        problems, warnings = np.check_values(with_entries(DATE="Thursday, October 15, 2026",
                                                          RECAP_DATE="October 14, 2026"))
        self.assertEqual(problems, [])
        self.assertTrue(any(w.startswith("RECAP_DATE=October 14, 2026 is not the date of this issue (Thursday, "
                                         "October 15, 2026)") for w in warnings), warnings)
        self.assertEqual(np.check_values(with_entries(DATE="Thursday, October 15, 2026", RECAP_DATE="October 15, 2026")),
                         ([], []))


class ExactValueTests(unittest.TestCase):
    def test_values_are_shown_exactly_as_typed(self):
        data = with_entries(stories=4, trends=6, radar=4,
                            STORY_4_TITLE="South Korea’s KOSPI Plunges 8% & 5 < 6 > 4 \"quoted\" 'too'",
                            TREND_6_NAME="A&B 3<4 \"q\" 'single' (AB)", TREND_6_CHANGE="-5.61%",
                            RADAR_4_LABEL="USDC Oversight Under MiCA & co")
        self.assertEqual(problems_of(data), [])
        _, page = email_for(data)
        text = visible_text(page)
        for value in ("South Korea’s KOSPI Plunges 8% & 5 < 6 > 4 \"quoted\" 'too'", "A&B 3<4 \"q\" 'single' (AB)",
                      "-5.61%", "USDC Oversight Under MiCA & co"):
            self.assertEqual(text.count(value), 1, value)
        self.assertNotIn("&amp;amp;", page)   # escaped once, never twice

    def test_markup_in_a_value_is_refused_and_could_never_run(self):
        data = with_entries(TREND_2_NAME="<script>alert(1)</script>", RADAR_1_LABEL="**bold**",
                            STORY_3_TITLE="Fees &amp; taxes")
        problems = problems_of(data)
        self.assertTrue(any("TREND_2_NAME contains the HTML tag <script>" in p for p in problems))
        self.assertTrue(any("RADAR_1_LABEL contains ** - bold is only possible" in p for p in problems))
        self.assertTrue(any("STORY_3_TITLE contains the HTML code &amp;" in p for p in problems))
        _, page = email_for(data)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_story_addresses_are_checked_and_encoded_for_any_story(self):
        self.assertTrue(any(p.startswith("STORY_5_URL must start with https://")
                            for p in problems_of(with_entries(stories=5, STORY_5_URL="http://coinedition.com/x"))))
        _, page = email_for(with_entries(stories=5, STORY_5_URL="https://coinedition.com/a b/?x=1&y=2"))
        self.assertEqual(TITLE.findall(page)[4], ("https://coinedition.com/a%20b/?x=1&amp;y=2", "Story 5 headline"))
        self.assertEqual(html.unescape(TITLE.findall(page)[4][0]), "https://coinedition.com/a%20b/?x=1&y=2")


if __name__ == "__main__":
    unittest.main()
