"""Subscriber files (.csv in different encodings/separators, .xlsx) and split_excel.py."""

import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support  # noqa: F401

import recipients as rc
import split_excel


def write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


class EmailTests(unittest.TestCase):
    def test_cleaning(self):
        for raw in ("  Sub0001@Example.COM ", "mailto:sub0001@example.com", "<sub0001@example.com>",
                    "sub0001@example.com ", "​sub0001@example.com", '"sub0001@example.com"'):
            self.assertEqual(rc.clean_email(raw), "sub0001@example.com", raw)

    def test_validity(self):
        for good in ("a@b.co", "john.smith+news@gmail.com", "o'brien@example.org"):
            self.assertTrue(rc.is_valid_email(good), good)
        for bad in ("", "not-an-email", "a@b.c", "two@@x.com", "spaces in@x.com", "x@-bad-.com",
                    "dot.@x.com", "trailing@x.com.", "no-tld@localhost", "a" * 65 + "@x.com"):
            self.assertFalse(rc.is_valid_email(rc.clean_email(bad)), bad)


class ReadTableTests(unittest.TestCase):
    def test_csv_utf8_with_bom_comma(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / "s.csv", "﻿Email,First Name,Last Name\na@x.com,José,Núñez\nb@x.com,NA,None\n")
            headers, rows, description = rc.read_table(path)
        self.assertEqual(headers, ["Email", "First Name", "Last Name"])
        self.assertEqual(rows[0]["First Name"], "José")
        self.assertEqual((rows[1]["First Name"], rows[1]["Last Name"]), ("NA", "None"))  # kept as text
        self.assertIn("comma", description)

    def test_csv_saved_by_excel_windows_encoding_semicolons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / "s.csv", "E-mail Address;Name\na@x.com;Zoë\n", encoding="cp1252")
            headers, rows, description = rc.read_table(path)
        self.assertEqual(rows[0]["Name"], "Zoë")
        self.assertEqual(rc.find_column(headers, "email"), "E-mail Address")
        self.assertIn("semicolon", description)

    def test_csv_tab_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / "s.csv", "EMAIL\tFIRSTNAME\tLASTNAME\na@x.com\tAna\tLee\n")
            headers, rows, _ = rc.read_table(path)
        self.assertEqual([rc.find_column(headers, w) for w in ("email", "name", "surname")],
                         ["EMAIL", "FIRSTNAME", "LASTNAME"])
        self.assertEqual(rows[0]["LASTNAME"], "Lee")

    def test_xlsx(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.xlsx"
            pd.DataFrame([{"Email": "a@x.com", "Name": "NA", "Surname": ""}]).to_excel(path, index=False)
            headers, rows, _ = rc.read_table(path)
        self.assertEqual(rows, [{"Email": "a@x.com", "Name": "NA", "Surname": ""}])

    def test_status_like_columns_are_spotted(self):
        self.assertEqual(rc.status_columns(["Email", "Status", "Name", "OPTIN_TIME", "Unsubscribed", "City"]),
                         ["Status", "OPTIN_TIME", "Unsubscribed"])


class SplitTests(unittest.TestCase):
    def run_split(self, tmp, *args):
        tmp = Path(tmp)
        patches = [mock.patch.object(split_excel, "PROJECT_DIR", tmp),
                   mock.patch.object(split_excel, "DATA_DIR", tmp / "data"),
                   mock.patch.object(split_excel, "BATCHES_DIR", tmp / "batches"),
                   mock.patch.object(split_excel, "REMOVED_REPORT", tmp / "logs" / "removed_subscribers.csv"),
                   mock.patch.object(split_excel, "SEND_LOG_FILE", tmp / "logs" / "production_send_log.csv"),
                   mock.patch.object(sys, "argv", ["split_excel.py", *args])]
        output = io.StringIO()
        for p in patches:
            p.start()
        try:
            with redirect_stdout(output):
                code = split_excel.main()
        except SystemExit as stop:
            code = stop.code
        finally:
            for p in patches:
                p.stop()
        return code, output.getvalue()

    def subscriber_csv(self):
        lines = ["Email,Name,Surname"]
        good = [f"sub{i:04d}@example-subscriber.test" for i in range(1, 4301)]
        lines += [f"{e},Tester,X" for e in good]
        lines += [f"{good[k * 211].upper()},Dup,{k}" for k in range(10)]
        lines += [f"  mailto:{good[k * 211 + 5]}  ,Dup,{k}" for k in range(10)]
        lines += ["not-an-email,Bad,", "a@b.c,Bad,", "two@@x.test,Bad,", "spaces in@x.test,Bad,",
                  "trailing@x.test.,Bad,", "x@-bad-.test,Bad,", "no-tld@localhost,Bad,", "plainaddress,Bad,"]
        lines += [",NoEmail,1", ",NoEmail,2", ",NoEmail,3", ",NoEmail,4", ",,", ",,", ",,"]
        return "\n".join(lines) + "\n", set(good)

    INVALID = ["not-an-email", "a@b.c", "two@@x.test", "spaces in@x.test", "trailing@x.test.", "x@-bad-.test",
               "no-tld@localhost", "plainaddress"]

    def batch_sizes(self, tmp):
        return [len(list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8-sig")))))
                for f in sorted((Path(tmp) / "batches").glob("batch_*.csv"))]

    def test_4335_row_csv_is_split_into_batches_of_300(self):
        text, good = self.subscriber_csv()
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "CE Newsletter Subscribers.csv", text)
            code, out = self.run_split(tmp)
            self.assertEqual(code, 0, out)
            files = sorted((Path(tmp) / "batches").glob("batch_*.csv"))
            seen, sizes = [], []
            for f in files:
                rows = list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8-sig"))))
                sizes.append(len(rows))
                seen += [r["Email"] for r in rows]
            removed = list(csv.DictReader(io.StringIO(
                (Path(tmp) / "logs" / "removed_subscribers.csv").read_text(encoding="utf-8-sig"))))
            flagged = list(csv.DictReader(io.StringIO(
                (Path(tmp) / "logs" / "invalid_addresses.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual(sizes, [300] * 14 + [108])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), good | set(self.INVALID))       # invalid ones KEPT, exactly as written
        reasons = sorted({r["Reason"].split(" (")[0] for r in removed})
        self.assertEqual(reasons, ["blank email", "duplicate"])     # nobody is left out for looking invalid
        self.assertEqual(len(removed), 20 + 4)
        self.assertEqual([r["Email"] for r in flagged], self.INVALID)
        self.assertIn("4,308 subscribers in 15 batches", out)

    def test_first_batch_can_be_smaller(self):
        text, _ = self.subscriber_csv()
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "subscribers.csv", text)
            code, out = self.run_split(tmp, "--first-batch-size", "280")
            self.assertEqual(code, 0, out)
            self.assertEqual(self.batch_sizes(tmp), [280] + [300] * 13 + [128])
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "subscribers.csv", text)
            self.assertEqual(self.run_split(tmp, "--first-batch-size", "301")[0], 1)

    def test_addresses_go_into_the_batches_exactly_as_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "subscribers.csv", "Email,Name\nAna.Lima@Example.COM,Ana\nbob.@gmail.com,Bob\n")
            code, out = self.run_split(tmp)
            self.assertEqual(code, 0, out)
            rows = list(csv.DictReader(io.StringIO(
                (Path(tmp) / "batches" / "batch_01.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual([r["Email"] for r in rows], ["Ana.Lima@Example.COM", "bob.@gmail.com"])
        self.assertIn("of which look invalid     : 1", out)

    def test_status_column_stops_unless_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "list.csv", "Email,Name,Status\na@x.com,A,subscribed\nb@x.com,B,unsubscribed\n")
            code, out = self.run_split(tmp)
            self.assertEqual(code, 1)
            self.assertIn("'Status': subscribed x1, unsubscribed x1", out)
            self.assertFalse((Path(tmp) / "batches").exists())
            code, out = self.run_split(tmp, "--ignore-status-column")
            self.assertEqual(code, 0, out)

    def test_more_than_one_file_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "a.csv", "Email\na@x.com\n")
            write(Path(tmp) / "data" / "b.csv", "Email\nb@x.com\n")
            code, out = self.run_split(tmp)
            self.assertEqual(code, 1)
            self.assertIn("more than one subscriber file", out)
            code, out = self.run_split(tmp, "--input", str(Path(tmp) / "data" / "b.csv"))
            self.assertEqual(code, 0, out)
            batch = (Path(tmp) / "batches" / "batch_01.csv").read_text(encoding="utf-8-sig")
            self.assertIn("b@x.com", batch)
            self.assertNotIn("a@x.com", batch)

    def test_existing_batches_need_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "data" / "a.csv", "Email\na@x.com\n")
            self.assertEqual(self.run_split(tmp)[0], 0)
            code, out = self.run_split(tmp)
            self.assertEqual(code, 1)
            self.assertIn("--overwrite", out)
            self.assertEqual(self.run_split(tmp, "--overwrite")[0], 0)


if __name__ == "__main__":
    unittest.main()
