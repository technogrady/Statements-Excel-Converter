"""Redaction tests.

Two things have to hold at once, and they pull against each other: the
holder's identity must be gone, and the statement's structure must
survive — a dump that has lost its section headers, column rules or
wrapped descriptions can't be used to fix the parser it was taken for.
"""
import re

from statement_parsers.redact import (
    ADDRESS_PLACEHOLDER,
    NAME_PLACEHOLDER,
    redact_pages,
    redact_text,
)

# The Capital City header: a promo block, then the holder's name and address
# in the left column with the fields the parser reads on the right.
CAPITALCITY_PAGE = """10000675255 002 30
Enroll in Making Cents & boost your savings when you use your personal
Capital City Bank Visa debit card to shop! Sign up today or learn more at
ccbg.com/makingcents. Terms and conditions apply.
EXAMPLE CONTRACTING LLC Date 1/31/24 Page 1
53 REDWOOD LN Primary Account XXXXXXX5255
CRAWFORDVILLE FL 32327-1961
CHECKING ACCOUNT
EVERYDAY CHECKING FOR BUSINESS Images 23
Account Number XXXXXXX5255 Statement Dates 1/01/24 thru 1/31/24
Previous Balance 10,282.45 Days in this Statement Period 31
5 Deposits/Credits 64,358.72 Avg Ledger Balance 36,437.38
Ending Balance 48,699.49
OTHER DEBITS
Date Description Amount
1/02 POS DEB 1529 12/29/23 22255700 67.00-
SUNSTOP 430
401 COLQUITT HWY
1/30 DBT CRD 2153 01/29/24 28091477 50.45-
ACE HOME CTR / NAPA
CRAWFORDVILLEFL
"""


class TestIdentityIsRemoved:
    def test_holder_name_and_street_and_city_are_gone(self):
        out = redact_text(CAPITALCITY_PAGE)
        assert "EXAMPLE CONTRACTING LLC" not in out
        assert "REDWOOD" not in out
        assert "32327" not in out
        assert "CRAWFORDVILLE FL" not in out
        assert NAME_PLACEHOLDER in out
        assert ADDRESS_PLACEHOLDER in out

    def test_every_long_digit_run_is_masked_to_its_shape(self):
        out = redact_text(CAPITALCITY_PAGE)
        assert "10000675255" not in out
        assert "###########" in out  # same length as the control number
        assert "22255700" not in out
        assert not re.search(r"(?<!\d)\d{7,}(?!\d)", out)

    def test_the_banks_own_mask_loses_its_last_four(self):
        out = redact_text(CAPITALCITY_PAGE)
        assert "5255" not in out
        assert "XXXXXXX####" in out

    def test_a_full_unmasked_account_number_is_masked(self):
        out = redact_text("Account number: 0123456789 ACME")
        assert "0123456789" not in out
        assert "##########" in out


class TestStructureSurvives:
    def test_section_headers_column_rules_and_figures_are_kept(self):
        out = redact_text(CAPITALCITY_PAGE)
        for keep in (
            "CHECKING ACCOUNT",
            "EVERYDAY CHECKING FOR BUSINESS Images 23",
            "Statement Dates 1/01/24 thru 1/31/24",
            "Previous Balance 10,282.45",
            "5 Deposits/Credits 64,358.72",
            "Ending Balance 48,699.49",
            "OTHER DEBITS",
            "Date Description Amount",
            "67.00-",
            "1/30 DBT CRD 2153 01/29/24",
        ):
            assert keep in out, keep

    def test_the_bank_signature_survives_so_routing_can_be_diagnosed(self):
        # Capital City prints no wordmark; the promo is the only text that
        # names the bank. Losing it would hide why a file didn't route.
        out = redact_text(CAPITALCITY_PAGE)
        assert "ccbg.com/makingcents" in out
        assert "Capital City Bank" in out

    def test_merchant_name_and_street_in_a_description_are_kept(self):
        # A wrapped description is a merchant, then its street — the lines
        # the wrap logic exists for. They are a shop's address, not the
        # holder's, and redacting them would hide the bug being diagnosed.
        out = redact_text(CAPITALCITY_PAGE)
        assert "SUNSTOP 430" in out
        assert "401 COLQUITT HWY" in out
        assert "ACE HOME CTR / NAPA" in out

    def test_line_count_is_unchanged(self):
        source = CAPITALCITY_PAGE.splitlines()
        assert len(redact_text(CAPITALCITY_PAGE).splitlines()) == len(source)

    def test_right_hand_field_survives_its_line_being_redacted(self):
        out = redact_text(CAPITALCITY_PAGE)
        assert f"{ADDRESS_PLACEHOLDER} Primary Account XXXXXXX####" in out
        assert f"{NAME_PLACEHOLDER} Date 1/31/24 Page 1" in out


class TestOtherLayouts:
    SERVISFIRST_PAGE = """ServisFirst Bank
0000 Example Place
Anytown, ST 00000
Date 8/30/22 Page 1
Primary Acct. XXXXXXXXXXXX5678
ACME EXAMPLE LLC
123 EXAMPLE ST
ANYTOWN ST 00000
BUSINESS CHECKING
Account Number XXXXXXXXXXXX5678 Statement Dates 7/30/22 thru 9/02/22
Previous Balance 20,000.00 Days in the statement period 35
"""

    def test_servisfirst_mailing_block_goes_and_the_summary_stays(self):
        out = redact_text(self.SERVISFIRST_PAGE)
        assert "ACME EXAMPLE LLC" not in out
        assert "123 EXAMPLE ST" not in out
        assert "ANYTOWN ST 00000" not in out
        assert "ServisFirst Bank" in out  # the signature that routes the file
        assert "BUSINESS CHECKING" in out
        assert "Statement Dates 7/30/22 thru 9/02/22" in out
        assert "Previous Balance 20,000.00" in out

    def test_a_date_left_of_a_page_marker_is_not_taken_for_a_name(self):
        # Wells Fargo prints 'January 31, 2022  Page 1 of 3' — the period end
        # date sits exactly where a holder name would.
        out = redact_text("January 31, 2022 Page 1 of 3")
        assert out == "January 31, 2022 Page 1 of 3"

    def test_a_statement_field_left_of_a_marker_is_not_taken_for_a_name(self):
        out = redact_text("Statement period activity summary Account number: 1234")
        assert NAME_PLACEHOLDER not in out
        assert "Statement period activity summary" in out


class TestEdges:
    def test_empty_and_blank_input(self):
        assert redact_text("") == ""
        assert redact_text("\n\n") == "\n\n"
        assert redact_pages([]) == []

    def test_pages_are_redacted_independently(self):
        pages = redact_pages([CAPITALCITY_PAGE, "53 REDWOOD LN Primary Account X5255"])
        assert "REDWOOD" not in pages[0]
        assert "REDWOOD" not in pages[1]

    def test_check_numbers_and_zip_length_digits_are_left_alone(self):
        # Four- and five-digit runs are check numbers and the like; masking
        # them would cost the parser's own fields for no privacy gain.
        out = redact_text("1/26 1573 138.00 1/23 1631 640.00")
        assert out == "1/26 1573 138.00 1/23 1631 640.00"


class TestDumpTextCli:
    """``--dump-text`` is the diagnostic path: it must print the extractors'
    own view of the file, redacted, and write nothing else."""

    def _run(self, target, capsys, *extra):
        import parse_statements

        rc = parse_statements.main([str(target), "--dump-text", *extra])
        assert rc == 0
        return capsys.readouterr().out

    def test_dumps_a_single_pdf_and_writes_no_workbook(self, tmp_path, capsys,
                                                       all_fixtures_dir):
        out = self._run(all_fixtures_dir / "capitalcity_checking_2021-12.pdf", capsys)
        assert "capitalcity_checking_2021-12.pdf" in out
        assert "extractor: pdfplumber" in out
        assert "CHECKING ACCOUNT" in out
        assert not list(tmp_path.glob("*.xlsx"))

    def test_output_is_redacted(self, capsys, all_fixtures_dir):
        out = self._run(all_fixtures_dir / "capitalcity_checking_2021-12.pdf", capsys)
        assert "EXAMPLE CONTRACTING LLC" not in out
        assert "ANYTOWN FL 00000-0000" not in out
        assert "XXXXXXX9012" not in out
        assert NAME_PLACEHOLDER in out or ADDRESS_PLACEHOLDER in out

    def test_pages_limits_the_dump(self, capsys, all_fixtures_dir):
        target = all_fixtures_dir / "capitalcity_checking_2021-12.pdf"
        assert self._run(target, capsys, "--pages", "1").count("----- page ") < (
            self._run(target, capsys).count("----- page ")
        )

    def test_a_file_with_no_readable_text_says_so_per_extractor(
        self, capsys, all_fixtures_dir
    ):
        out = self._run(all_fixtures_dir / "cid_garbage_text_layer.pdf", capsys)
        assert "(cid:" in out  # the dump shows what the extractor really gave
        assert "0 with any text" not in out  # ...it is text, just unreadable
