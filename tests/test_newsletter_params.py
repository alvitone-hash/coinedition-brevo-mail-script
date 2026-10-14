"""Content file -> Brevo params: mapping, validation, colours, safe HTML, URLs."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import support  # noqa: F401  (switches off the network, sets the import path)
from support import good_data, write_data_file

import newsletter_params as np

TODAY = date(2026, 10, 14)


def problems_for(**changes):
    data = good_data(TODAY)
    data.update(changes)
    return np.check_newsletter_data(data, TODAY)[0]


class MappingTests(unittest.TestCase):
    def test_every_param_has_its_own_data_key(self):
        self.assertEqual(len(np.DATA_KEYS), len(set(np.DATA_KEYS)))
        params = np.build_params(good_data(TODAY), "Alvitone", TODAY)
        self.assertEqual(len(params), 54)
        self.assertEqual(params["first_name"], "Alvitone")

    def test_story_paragraphs_become_one_description(self):
        data = dict(good_data(TODAY), STORY_2_P1="A", STORY_2_P2="**B**", STORY_2_P3="")
        self.assertEqual(np.build_params(data, "x", TODAY)["story_2_description"], "A<br /><br /><strong>B</strong>")

    def test_email_subject_is_the_first_story_heading_as_typed(self):
        data = dict(good_data(TODAY), STORY_1_TITLE="Bitcoin Rebounds as South Korean Markets Stabilise – Tuesday’s Low")
        self.assertEqual(np.email_subject(data), "Bitcoin Rebounds as South Korean Markets Stabilise – Tuesday’s Low")

    def test_title_and_intro_default_to_the_design_wording(self):
        params = np.build_params(dict(good_data(TODAY), NEWSLETTER_TITLE="", INTRO_HEADING="", INTRO_DESCRIPTION=""),
                                 "x", TODAY)
        self.assertEqual((params["newsletter_title"], params["intro_heading"]),
                         ("DAILY CRYPTO MARKET RECAP", "24-Hour Crypto Recap"))
        self.assertEqual(params["intro_description"],
                         "Here’s what happened across the crypto market over the past 24 hours.")

    def test_old_image_keys_are_ignored_with_a_note(self):
        problems, warnings = np.check_newsletter_data(
            dict(good_data(TODAY), HERO_IMAGE_URL="https://coinedition.com/x.webp"), TODAY)
        self.assertEqual(problems, [])
        self.assertTrue(any("images are fixed in the template" in w for w in warnings))
        self.assertNotIn("hero_image_url", np.build_params(good_data(TODAY), "x", TODAY))

    def test_editing_the_file_changes_the_param_on_the_next_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "newsletter_data.txt"
            data = good_data(TODAY)
            write_data_file(path, data)
            first, _ = np.read_newsletter_data(path)
            self.assertEqual(np.build_params(first, "x", TODAY)["btc_price"], "$84,681.83")
            data["BTC_PRICE"] = "$90,000.00"
            write_data_file(path, data)
            second, _ = np.read_newsletter_data(path)
            self.assertEqual(np.build_params(second, "x", TODAY)["btc_price"], "$90,000.00")

    def test_designer_names_are_accepted_as_the_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.txt"
            path.write_text("GLOBAL_MARKET_CAP=$3T\nGLOBAL_MARKET_CAP_CHANGE=+1%\nSTORY_1_DESCRIPTION=Text\n"
                            "TRENDING_1_NAME=Sui\nRADAR_1_TITLE=Label\nRADAR_1_DESCRIPTION=More\n", encoding="utf-8")
            data, problems = np.read_newsletter_data(path)
        self.assertEqual(problems, [])
        self.assertEqual((data["GLOBAL_CAP"], data["GLOBAL_CHANGE"], data["STORY_1_P1"]), ("$3T", "+1%", "Text"))
        self.assertEqual((data["TREND_1_NAME"], data["RADAR_1_LABEL"], data["RADAR_1_TEXT"]), ("Sui", "Label", "More"))

    def test_empty_date_becomes_the_sending_day(self):
        params = np.build_params(good_data(TODAY), "x", TODAY)
        self.assertEqual(params["date"], "Wednesday, October 14, 2026")


class ValidationTests(unittest.TestCase):
    def test_complete_real_content_passes(self):
        problems, warnings = np.check_newsletter_data(good_data(TODAY), TODAY)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])

    def test_content_must_be_confirmed_for_today(self):
        self.assertTrue(any("READY_TO_SEND_ON is empty" in p for p in problems_for(READY_TO_SEND_ON="")))
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        self.assertTrue(any("but today is" in p for p in problems_for(READY_TO_SEND_ON=yesterday)))
        self.assertTrue(any("must be a date" in p for p in problems_for(READY_TO_SEND_ON="14/10/2026")))

    def test_sample_and_placeholder_content_is_refused(self):
        self.assertTrue(any("sample" in p for p in problems_for(STORY_1_P1="Sample description")))
        self.assertTrue(any("sample" in p for p in problems_for(RADAR_2_TEXT="Lorem ipsum dolor")))
        self.assertTrue(any("placeholder" in p for p in
                            problems_for(STORY_1_URL="https://placehold.co/1120x280/png?text=Story")))
        self.assertTrue(any("placeholder" in p for p in problems_for(STORY_2_URL="https://example.com/x")))

    def test_readiness_is_checked_apart_from_the_values_but_production_gets_both(self):
        data = dict(good_data(TODAY), READY_TO_SEND_ON="", GLOBAL_CAP="$2.68T (sample - not verified)")
        readiness = np.readiness_problems(data, TODAY)
        self.assertEqual(len(readiness), 2)
        self.assertTrue("READY_TO_SEND_ON is empty" in readiness[0] and "GLOBAL_CAP is still sample" in readiness[1])
        self.assertEqual(np.check_values(data), ([], []))
        self.assertEqual(np.check_newsletter_data(data, TODAY), (readiness, []))

    def test_value_checks_never_depend_on_readiness(self):
        data = dict(good_data(TODAY), READY_TO_SEND_ON="", TREND_1_NAME="Sui (sample)",
                    STORY_1_URL="http://coinedition.com/x", BTC_PRICE="")
        problems, _ = np.check_values(data)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("BTC_PRICE is required" in p for p in problems))
        self.assertTrue(any("must start with https://" in p for p in problems))
        self.assertEqual(np.check_newsletter_data(data, TODAY)[0], np.readiness_problems(data, TODAY) + problems)

    def test_required_values_must_be_present(self):
        data = good_data(TODAY)
        del data["BTC_PRICE"]
        problems = np.check_newsletter_data(data, TODAY)[0]
        self.assertTrue(any("BTC_PRICE is required" in p for p in problems))
        self.assertTrue(any("STORY_3_TITLE is required" in p for p in problems_for(STORY_3_TITLE="")))
        self.assertEqual([p for p in problems_for(STORY_1_URL="", STORY_2_URL="", STORY_3_URL="") if "URL" in p],
                         [])   # the story titles are no longer links, so the article addresses may be empty
        self.assertTrue(any("no number" in p for p in problems_for(ETH_PRICE="soon")))

    def test_every_row_of_the_design_is_required(self):
        self.assertTrue(any("TREND_4_CHANGE is required" in p for p in problems_for(TREND_4_CHANGE="")))
        self.assertTrue(any("RADAR_3_TEXT is required" in p for p in problems_for(RADAR_3_TEXT="")))
        self.assertEqual(problems_for(STORY_1_P2="", STORY_1_P3="", DATE="", BTC_COLOR=""), [])

    def test_html_in_values_is_refused(self):
        self.assertTrue(any("HTML tag" in p for p in problems_for(STORY_1_P1="<script>alert(1)</script>")))
        self.assertTrue(any("HTML code" in p for p in problems_for(STORY_1_TITLE="Fees &amp; taxes")))


class ColourTests(unittest.TestCase):
    def test_colour_follows_the_sign(self):
        for change, colour in [("+0.35%", np.RISE_COLOR), ("-0.85%", np.FALL_COLOR), ("−1.2%", np.FALL_COLOR),
                               ("–0.5%", np.FALL_COLOR), ("0.00%", np.FLAT_COLOR), ("0%", np.FLAT_COLOR),
                               ("+0.00%", np.FLAT_COLOR), ("-1,250.5%", np.FALL_COLOR)]:
            self.assertEqual(np.color_value("", change), colour, change)

    def test_design_colours_are_used(self):
        self.assertEqual((np.RISE_COLOR, np.FALL_COLOR), ("#0B7A44", "#C8102E"))

    def test_text_and_colour_cannot_disagree(self):
        self.assertTrue(any("disagrees" in p for p in problems_for(BNB_CHANGE="+0.35%", BNB_COLOR="red")))
        self.assertEqual(problems_for(BNB_CHANGE="+0.35%", BNB_COLOR="green"), [])
        self.assertTrue(any("not allowed" in p for p in problems_for(BNB_COLOR="#123456")))

    def test_change_needs_a_sign_and_a_number(self):
        self.assertTrue(any("needs a + or - sign" in p for p in problems_for(SOL_CHANGE="0.92%")))
        self.assertTrue(any("not a percentage" in p for p in problems_for(SOL_CHANGE="up a bit")))

    def test_trending_changes_may_rise_or_fall_but_need_a_sign(self):
        self.assertEqual([p for p in problems_for(TREND_2_CHANGE="-3.0%") if "TREND_2_CHANGE" in p], [])
        self.assertTrue(any("needs a + or - sign" in p for p in problems_for(TREND_2_CHANGE="3.0%")))


class SafeHtmlTests(unittest.TestCase):
    def test_bold_markup_becomes_strong_and_everything_else_is_escaped(self):
        self.assertEqual(np.rich_text_html("the full **$3.8 million** & more"),
                         "the full <strong>$3.8 million</strong> &amp; more")
        self.assertEqual(np.rich_text_html("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")
        self.assertEqual(np.rich_text_html("**<img src=x onerror=alert(1)>**"),
                         "<strong>&lt;img src=x onerror=alert(1)&gt;</strong>")

    def test_rich_params_are_built_as_safe_html(self):
        params = np.build_params(good_data(TODAY), "x", TODAY)
        self.assertIn("<strong>$3.8 million</strong> in stolen funds.<br /><br />The investigation",
                      params["story_1_description"])
        self.assertIn("<strong>0.2% crypto transaction tax</strong>", params["radar_3_text"])
        self.assertEqual(set(np.RICH_TEXT_PARAMS) & {"story_1_title", "first_name", "radar_1_label"}, set())

    def test_plain_text_values_are_sent_unescaped_for_brevo_to_escape(self):
        params = np.build_params(dict(good_data(TODAY), STORY_1_TITLE="Fees & Taxes"), "O'Brien", TODAY)
        self.assertEqual(params["story_1_title"], "Fees & Taxes")
        self.assertEqual(params["first_name"], "O'Brien")

    def test_broken_bold_markup_is_refused(self):
        self.assertTrue(any("odd number of **" in p for p in problems_for(STORY_1_P1="the **$3.8 million")))
        self.assertTrue(any("****" in p for p in problems_for(STORY_1_P1="the **** bit")))
        self.assertTrue(any("bold is only possible" in p for p in problems_for(STORY_1_TITLE="**Big** news")))


class UrlTests(unittest.TestCase):
    def test_only_https_web_addresses(self):
        self.assertTrue(any("https://" in p for p in problems_for(STORY_1_URL="http://coinedition.com/x")))
        self.assertTrue(any("not a web address" in p for p in problems_for(STORY_1_URL="javascript:alert(1)")))
        self.assertTrue(any("not allowed" in p for p in problems_for(STORY_1_URL='https://coinedition.com/"x')))

    def test_spaces_are_encoded(self):
        params = np.build_params(dict(good_data(TODAY), STORY_1_URL="https://coinedition.com/a b/"), "x", TODAY)
        self.assertEqual(params["story_1_url"], "https://coinedition.com/a%20b/")


if __name__ == "__main__":
    unittest.main()
