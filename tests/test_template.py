"""The corrected template: the original design with Brevo placeholders, real links and unsubscribe."""

import re
import unittest
from collections import Counter
from datetime import date
from html import escape
from html.parser import HTMLParser

import support  # noqa: F401
from support import MASTER_TEMPLATE, ORIGINAL_TEMPLATE, good_data

import newsletter_params as np
import template_check as tc

TODAY = date(2026, 10, 14)
MASTER = MASTER_TEMPLATE.read_text(encoding="utf-8")
ORIGINAL = ORIGINAL_TEMPLATE.read_text(encoding="utf-8")

# The designer's temporary names -> the param that now fills that spot.
DESIGNER_NAMES = {"DATE": "date", "FIRST_NAME": "first_name", "GLOBAL_MARKET_CAP": "global_cap",
                  "GLOBAL_MARKET_CAP_CHANGE": "global_change",
                  **{f"{c}_{p}": f"{c.lower()}_{p.lower()}" for c in ("BTC", "ETH", "BNB", "XRP", "SOL")
                     for p in ("PRICE", "CHANGE")},
                  **{f"STORY_{n}_TITLE": f"story_{n}_title" for n in (1, 2, 3)},
                  **{f"STORY_{n}_DESCRIPTION": f"story_{n}_description" for n in (1, 2, 3)},
                  **{f"TRENDING_{n}_{p}": f"trend_{n}_{p.lower()}" for n in (1, 2, 3, 4) for p in ("NAME", "CHANGE")},
                  **{f"RADAR_{n}_TITLE": f"radar_{n}_label" for n in (1, 2, 3)},
                  **{f"RADAR_{n}_DESCRIPTION": f"radar_{n}_text" for n in (1, 2, 3)}}
IMAGES = re.compile(r'<img src="[^"]*"')


def params(**changes):
    return np.build_params(dict(good_data(TODAY), **changes), "Alvitone", TODAY)


class Page(HTMLParser):
    """Visible text, image addresses and style attributes of an email."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text, self.images, self.styles, self._skip = [], [], Counter(), 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "title", "head"):
            self._skip += 1
        if tag == "img":
            self.images.append(a.get("src"))
        if a.get("style"):
            self.styles[a["style"]] += 1

    def handle_endtag(self, tag):
        if tag in ("style", "title", "head"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.text.append(" ".join(data.split()))


class TemplateRuleTests(unittest.TestCase):
    def test_corrected_template_passes_every_check(self):
        problems, warnings = tc.check_template(MASTER, params())
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])
        self.assertEqual(tc.check_rendered(tc.render(MASTER, params())), [])

    def test_every_placeholder_is_a_param_the_code_sends_and_every_param_is_used(self):
        self.assertEqual(set(tc.placeholders(MASTER)), set(params()))

    def test_images_are_exactly_the_originals(self):
        self.assertEqual(IMAGES.findall(MASTER), IMAGES.findall(ORIGINAL))
        self.assertEqual(len(IMAGES.findall(tc.COMMENT.sub("", MASTER))), 4)  # logo x2, hero, partner

    def test_no_logic_tags_so_nothing_stray_shows_in_a_browser(self):
        self.assertNotIn("{%", MASTER)

    def test_no_temporary_or_wrong_format_tags_left(self):
        self.assertEqual(re.findall(r"\{\{\s*[A-Z][A-Z0-9_]*\s*\}\}", MASTER), [])
        self.assertNotIn('href="#"', MASTER)

    def test_unsubscribe_uses_brevos_documented_tag(self):
        self.assertEqual(len(re.findall(r'<a href="\{\{ unsubscribe \}\}"[^>]*>Unsubscribe</a>', MASTER)), 1)

    def test_safe_filter_only_on_the_safe_html_values(self):
        safe = set(re.findall(r"\{\{ params\.([a-z0-9_]+)\|safe \}\}", MASTER))
        self.assertEqual(safe, set(np.RICH_TEXT_PARAMS))

    def test_original_upload_is_detected_as_broken(self):
        problems, _ = tc.check_template(ORIGINAL, params())
        self.assertTrue(any("not in Brevo's params format" in p for p in problems))
        self.assertTrue(any("Unsubscribe" in p for p in problems))

    def test_checker_catches_common_mistakes(self):
        bad = MASTER.replace("{{ params.btc_price }}", "{{ params.BTC_PRICE }}")
        self.assertTrue(any("'BTC_PRICE'" in p for p in tc.check_template(bad, params())[0]))
        bad = MASTER.replace("{{ params.story_1_title }}", "{{ params.story_1_title|safe }}")
        self.assertTrue(any("|safe is only allowed" in p for p in tc.check_template(bad, params())[0]))
        bad = MASTER.replace("{{ params.story_1_description|safe }}", "{{ params.story_1_description }}")
        self.assertTrue(any("needs |safe" in p for p in tc.check_template(bad, params())[0]))
        bad = MASTER.replace("<!-- Bitcoin -->", "<!-- Bitcoin {{ params.btc_price }} -->")
        self.assertTrue(any("inside an HTML comment" in p for p in tc.check_template(bad, params())[0]))
        bad = MASTER.replace("{{ params.date }}", "{% if params.date %}{{ params.date }}")
        self.assertTrue(any("never closed" in p for p in tc.check_template(bad, params())[0]))
        bad = MASTER.replace('href="{{ unsubscribe }}"', 'href="#"')
        self.assertTrue(any("Unsubscribe" in p for p in tc.check_template(bad, params())[0]))


class RenderTests(unittest.TestCase):
    def test_links_and_images_are_real_https_addresses(self):
        email = tc.render(MASTER, params())
        links, images = tc.link_report(email)
        hrefs = [h for h, _ in links]
        self.assertEqual(len(hrefs), 25)
        self.assertTrue(all(h.startswith("https://") for h in hrefs))
        for wanted in ("https://coinedition.com/near-intents-3-8-million-exploit-what-users-should-know-and-do/",
                       "https://unchainedsummit.com/india/", "https://coinedition.com/privacy-policy/",
                       "https://twitter.com/CoinEdition", "https://www.linkedin.com/company/coinedition/",
                       "https://t.me/coinedition", "https://www.instagram.com/coinedition_official/",
                       "https://coinedition.com/market/price-prediction/", "https://coinedition.com/tag/xrp-news/",
                       tc.PREVIEW_UNSUBSCRIBE):
            self.assertIn(wanted, hrefs)
        self.assertEqual([src for src, _ in images], [
            "https://coinedition.com/wp-content/uploads/2022/07/CE-LOGO-WITH-TOG-02-min.png",
            "https://coinedition.com/wp-content/uploads/2026/10/coinedition-cover-reddit01.webp",
            "https://coinedition.com/wp-content/uploads/2026/10/unchained-summit-india02.webp",
            "https://coinedition.com/wp-content/uploads/2022/07/CE-LOGO-WITH-TOG-02-min.png"])

    def test_story_titles_link_to_their_articles(self):
        email = tc.render(MASTER, params())
        for n in (1, 2, 3):
            url = escape(params()[f"story_{n}_url"])
            title = escape(params()[f"story_{n}_title"])
            self.assertRegex(email, rf'<a href="{re.escape(url)}"[^>]*>{re.escape(title)}</a>')

    def test_optional_values_empty_still_give_a_clean_email(self):
        empty = {key: "" for key in np.OPTIONAL_KEYS}
        email = tc.render(MASTER, np.build_params(dict(good_data(TODAY), **empty), "there", TODAY))
        self.assertEqual(tc.check_rendered(email), [])
        self.assertIn("DAILY CRYPTO MARKET RECAP", email)      # empty title -> the design's title
        self.assertIn("24-Hour Crypto Recap", email)
        self.assertNotIn("<br /><br />\n", email)              # no empty paragraph at the end of a story

    def test_values_are_escaped_except_the_safe_html(self):
        email = tc.render(MASTER, params(STORY_1_TITLE="Fees & <Taxes>"))
        self.assertIn("Fees &amp; &lt;Taxes&gt;", email)
        self.assertIn("<strong>$3.8 million</strong>", email)

    def test_change_colours_follow_the_values(self):
        email = tc.render(MASTER, params(BNB_CHANGE="+0.35%", ETH_CHANGE="0.00%"))
        self.assertIn("color:#0B7A44; mso-line-height-rule:exactly;\">\n                  <!-- PDF: -0.7% --> +0.35%", email)
        self.assertIn("color:#7A8396; mso-line-height-rule:exactly;\">\n                  <!-- PDF: -1.4% --> 0.00%", email)
        self.assertIn("color:#C8102E; mso-line-height-rule:exactly;\">\n                  <!-- PDF: -0.6% --> -0.6%", email)


class DesignUnchangedTests(unittest.TestCase):
    """Filled in, the corrected template must look exactly like the original design."""

    def original_filled(self, values):
        def fill(match):
            param = DESIGNER_NAMES[match.group(1)]
            return values[param] if param in np.RICH_TEXT_PARAMS else escape(values[param])
        head, body = ORIGINAL.split("<body", 1)
        return head + "<body" + re.sub(r"\{\{([A-Z0-9_]+)\}\}", fill, body)

    def test_same_text_images_and_styles_as_the_original(self):
        values = params()  # every 24h change negative, like the original's hard-coded red
        corrected, original = Page(tc.render(MASTER, values)), Page(self.original_filled(values))
        self.assertEqual(corrected.text, original.text)
        self.assertEqual(corrected.images, original.images)
        self.assertEqual(original.styles - corrected.styles, Counter())
        self.assertEqual(corrected.styles - original.styles,
                         Counter({"color:#181C47; text-decoration:none;": 3}))  # the 3 linked story titles


if __name__ == "__main__":
    unittest.main()
