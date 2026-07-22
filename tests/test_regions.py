"""Ground-truth assertions for the Regions sample (synthetic fixture
regions_checking_2022-01.pdf — all values are fabricated placeholders)."""
from datetime import date
from decimal import Decimal

from statement_parsers import regions
from statement_parsers.base import TX_DEPOSIT, TX_FEE, TX_INTEREST, TX_WITHDRAWAL


class TestRegionsGroundTruth:
    def test_identity(self, regions_stmt):
        s = regions_stmt
        assert s.bank == "Regions"
        assert s.account_last4 == "1000"
        assert s.account_number_full is None  # sample number is masked
        assert s.account_label == "BUSINESS INTEREST CHECKING"

    def test_period_and_balances(self, regions_stmt):
        s = regions_stmt
        assert s.period_start == date(2021, 12, 15)
        assert s.period_end == date(2022, 1, 14)
        assert s.opening_balance == Decimal("10000.00")
        assert s.closing_balance == Decimal("9252.00")

    def test_exactly_seven_transactions(self, regions_stmt):
        assert len(regions_stmt.transactions) == 7

    def test_deposit(self, regions_stmt):
        deposits = [t for t in regions_stmt.transactions if t.tx_type == TX_DEPOSIT]
        assert len(deposits) == 1
        assert deposits[0].date == date(2022, 1, 3)
        assert deposits[0].amount == Decimal("5000.00")

    def test_interest(self, regions_stmt):
        interest = [t for t in regions_stmt.transactions if t.tx_type == TX_INTEREST]
        assert len(interest) == 1
        assert interest[0].date == date(2022, 1, 10)
        assert interest[0].amount == Decimal("2.00")

    def test_withdrawals_with_year_split(self, regions_stmt):
        withdrawals = [t for t in regions_stmt.transactions if t.tx_type == TX_WITHDRAWAL]
        got = sorted((t.date, t.amount) for t in withdrawals)
        assert got == [
            (date(2021, 12, 18), Decimal("-1000.00")),
            (date(2021, 12, 20), Decimal("-1500.00")),
            (date(2021, 12, 22), Decimal("-750.00")),
            (date(2021, 12, 22), Decimal("-500.00")),
            (date(2021, 12, 31), Decimal("-2000.00")),
        ]
        # December dates resolve to 2021, January dates to 2022
        assert all(t.date.year == 2021 for t in withdrawals)

    def test_reconciles_exactly(self, regions_stmt):
        s = regions_stmt
        assert s.reconciled is True
        total = sum(t.amount for t in s.transactions)
        assert s.opening_balance + total == s.closing_balance

    def test_no_crosscheck_mismatch_notes(self, regions_stmt):
        # Section totals and SUMMARY figures must agree with parsed sums.
        assert regions_stmt.notes == []

    def test_daily_balance_and_disclosures_do_not_leak(self, regions_stmt):
        # The DAILY BALANCE SUMMARY table and 'Easy Steps to Balance Your
        # Account' page contain date+amount shaped lines; none may import.
        descriptions = " ".join(t.description for t in regions_stmt.transactions)
        assert "Balance" not in descriptions
        assert "999.99" not in descriptions


class TestRegionsPersonalLayout:
    """Regions personal/LifeGreen statements (synthetic fixture
    regions_personal_2022-02.pdf). These reproduce the extraction quirks the
    business fixtures don't: 'through' glued to the next month, and per-page
    footer/header furniture landing inside an open transaction section on the
    multi-page path."""

    def test_glued_through_period_parses(self, regions_personal_stmt):
        s = regions_personal_stmt
        # "January 11, 2022 throughFebruary 7, 2022" — no space after 'through'.
        assert s.period_start == date(2022, 1, 11)
        assert s.period_end == date(2022, 2, 7)
        assert s.account_last4 == "7777"
        assert s.account_label == "LIFEGREEN BUSINESS CHECKING"

    def test_reconciles_exactly(self, regions_personal_stmt):
        s = regions_personal_stmt
        assert s.reconciled is True
        total = sum(t.amount for t in s.transactions)
        assert s.opening_balance + total == s.closing_balance

    def test_no_reconciliation_or_crosscheck_mismatch_notes(self, regions_personal_stmt):
        # Section totals and SUMMARY figures agree with parsed sums, and the
        # statement reconciles — so no mismatch/failure notes. (Unrecognized
        # page furniture may still produce informational 'stray line' notes;
        # those are asserted separately below.)
        bad = [
            n for n in regions_personal_stmt.notes
            if "!=" in n or "Reconciliation failed" in n
        ]
        assert bad == []

    def test_unrecognized_furniture_is_noted_not_glued(self, regions_personal_stmt):
        # Page-2 header lines that aren't recognized noise (the branch-office
        # line, the standalone branch code) are reported as ignored stray lines
        # rather than glued onto the prior transaction — a visible, safe signal.
        stray = [n for n in regions_personal_stmt.notes if "stray line" in n]
        assert any("Example Branch Office" in n for n in stray)

    def test_all_sections_parsed(self, regions_personal_stmt):
        by_type = {}
        for t in regions_personal_stmt.transactions:
            by_type.setdefault(t.tx_type, []).append(t)
        assert len(by_type[TX_DEPOSIT]) == 4
        assert len(by_type[TX_INTEREST]) == 1
        assert len(by_type[TX_WITHDRAWAL]) == 2
        assert len(by_type["Fee"]) == 1

    def test_footer_and_header_furniture_not_glued(self, regions_personal_stmt):
        # The per-page footer and the reprinted page-2 header block (including
        # the branch-office line, which is not recognized page noise) must not
        # leak into any transaction description.
        joined = " ".join(t.description for t in regions_personal_stmt.transactions)
        for leak in (
            "banking needs",
            "Banking With Regions",
            "Member FDIC",
            "Example Branch",
            "Page 2",
            "Enclosures",
            "9,000.00",  # a DAILY BALANCE SUMMARY figure
        ):
            assert leak not in joined, f"{leak!r} leaked into a description"

    def test_last_deposit_before_page_break_is_clean(self, regions_personal_stmt):
        # 01/15 is the last deposit on page 1; the page-1 footer follows it and
        # the page-2 header precedes the next row — its description must stay put.
        d = next(
            t for t in regions_personal_stmt.transactions
            if t.date == date(2022, 1, 15)
        )
        assert d.description == "Square Inc 220115p2Example Merchant Port"


class TestRegionsLayoutVariants:
    PAGE = """Regions Bank
{account_lines}
BUSINESS CHECKINGS
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $100.00
Deposits & Credits $50.00 +
Ending Balance $150.00
DEPOSITS & CREDITS
01/20 Deposit 50.00
Total Deposits & Credits $50.00
DAILY BALANCE SUMMARY
"""

    def test_account_number_on_next_line(self):
        text = self.PAGE.format(account_lines="ACCOUNT #\nxxxxxx1234")
        stmt = regions.parse([text], "f.pdf")[0]
        assert stmt.account_last4 == "1234"
        assert stmt.account_number_full is None

    def test_full_unmasked_account_number(self):
        text = self.PAGE.format(account_lines="ACCOUNT # 0056781000")
        stmt = regions.parse([text], "f.pdf")[0]
        assert stmt.account_number_full == "0056781000"
        assert stmt.account_last4 == "1000"

    def test_spaced_out_of_sequence_markers_in_two_check_columns(self):
        text = """Regions Bank
ACCOUNT # xxxxxx1234
BUSINESS CHECKINGS
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $10,000.00
Checks $8,874.78
Ending Balance $1,125.22
CHECKS
Date Check No. Amount Date Check No. Amount
01/16 8375 * 5,294.78 01/24 8611 * 3,580.00
Total Checks $8,874.78
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]

        assert [(t.check_no, t.amount) for t in stmt.transactions] == [
            ("8375", Decimal("-5294.78")),
            ("8611", Decimal("-3580.00")),
        ]
        assert all("out of sequence" in t.description for t in stmt.transactions)
        assert stmt.reconciled is True
        assert stmt.notes == []

    def test_checks_without_check_numbers(self):
        # Counter/substitute checks print with a blank check-number column, so
        # extraction yields bare ``MM/DD amount`` pairs — standalone, sharing a
        # two-column row with a numbered check, and two-per-row. All must be
        # captured (dropping one silently unbalances the CHECKS total).
        text = """Regions Bank
ACCOUNT # xxxxxx1234
BUSINESS CHECKINGS
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $10,000.00
Checks $3,343.15
Ending Balance $6,656.85
CHECKS
Date Check No. Amount Date Check No. Amount
01/16 8375 100.00 01/24 60.00
01/28 33.15 02/01 3,150.00
Total Checks $3,343.15
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]

        assert [(t.check_no, t.amount) for t in stmt.transactions] == [
            ("8375", Decimal("-100.00")),
            (None, Decimal("-60.00")),
            (None, Decimal("-33.15")),
            (None, Decimal("-3150.00")),
        ]
        # Numberless checks get a plain "Check" description, no fabricated number.
        assert [t.description for t in stmt.transactions] == [
            "Check 8375",
            "Check",
            "Check",
            "Check",
        ]
        assert stmt.reconciled is True
        assert stmt.notes == []

    def test_checks_converted_to_electronic_withdrawals_section(self):
        # Regions prints checks a merchant converted to ACH in their own
        # section; the row is 'MM/DD checkno description amount' and the debit
        # must be counted (dropping it broke reconciliation by that amount).
        text = """Regions Bank
ACCOUNT # xxxxxx1234
LIFEGREEN BUSINESS CHECKING
October 15, 2024 through November 14, 2024
SUMMARY
Beginning Balance $100.00
Ending Balance $65.24
CHECKS CONVERTED BY MERCHANT TO ELECTRONIC WITHDRAWALS
Date Check No. Description of Check Payment Amount
10/17 9749 Advance Auto Par Advance Au 14003100000380 34.76
Checks that are converted by a merchant to an electronic withdrawal are not returned to Regions. Therefore, if you receive
check enclosures or check images with your monthly statement, checks listed above are not included with this statement.
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]

        assert len(stmt.transactions) == 1
        tx = stmt.transactions[0]
        assert tx.amount == Decimal("-34.76")
        assert tx.date == date(2024, 10, 17)
        # The trailing disclaimer prose must not glue onto the description.
        assert "converted" not in tx.description.lower()
        assert stmt.reconciled is True
        assert stmt.notes == []

    def test_returned_checks_section_credited_not_fee_debited(self):
        # 'RETURNED CHECKS' is its own section of credits. Before it was
        # recognized, its rows were absorbed into FEES and booked as debits —
        # a returned CREDIT counted as a fee DEBIT, doubling the recon error.
        text = """Regions Bank
ACCOUNT # xxxxxx1234
LIFEGREEN BUSINESS CHECKING
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $1,000.00
Fees $7.75 -
Ending Balance $2,492.25
FEES
01/20 Cash Deposit Fee 7.75
RETURNED CHECKS
01/22 Credit-Returned Ck# 1234 500.00
01/25 Credit-Returned Ck# 5678 1,000.00
Total Returned Checks $1,500.00
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]

        fees = [t for t in stmt.transactions if t.tx_type == TX_FEE]
        credits = [t for t in stmt.transactions if t.tx_type == TX_DEPOSIT]
        assert [t.amount for t in fees] == [Decimal("-7.75")]
        assert sorted(t.amount for t in credits) == [Decimal("500.00"), Decimal("1000.00")]
        assert stmt.reconciled is True
        # FEES cross-check now agrees ($7.75), and the returned-check total
        # matches — so no mismatch notes at all.
        assert stmt.notes == []

    def test_label_captured_verbatim_not_hardcoded(self):
        text = self.PAGE.format(account_lines="ACCOUNT # xxxxxx1234")
        stmt = regions.parse([text], "f.pdf")[0]
        assert stmt.account_label == "BUSINESS CHECKINGS"

    def test_checks_section_and_fees(self):
        text = """Regions Bank
ACCOUNT # xxxxxx1234
BUSINESS CHECKINGS
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $1,000.00
Ending Balance $233.00
CHECKS
Date Check No. Amount Date Check No. Amount
01/16 101 500.00 01/20 103* 250.00
FEES
01/31 Monthly Maintenance Fee 17.00
Total Fees $17.00
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]
        checks = [t for t in stmt.transactions if t.tx_type == "Check"]
        fees = [t for t in stmt.transactions if t.tx_type == "Fee"]
        assert [(c.check_no, c.amount) for c in checks] == [
            ("101", Decimal("-500.00")),
            ("103", Decimal("-250.00")),
        ]
        assert "out of sequence" in checks[1].description
        assert fees[0].amount == Decimal("-17.00")
        assert stmt.reconciled

    def test_automatic_transfer_sign_inferred_from_reconciliation(self):
        text = """Regions Bank
ACCOUNT # xxxxxx1234
BUSINESS CHECKINGS
January 15, 2022 through February 14, 2022
SUMMARY
Beginning Balance $100.00
Ending Balance $400.00
AUTOMATIC TRANSFERS
01/20 Transfer from Savings 300.00
DAILY BALANCE SUMMARY
"""
        stmt = regions.parse([text], "f.pdf")[0]
        assert stmt.reconciled
        assert stmt.transactions[0].amount == Decimal("300.00")
        assert any("sign inferred" in n for n in stmt.notes)
