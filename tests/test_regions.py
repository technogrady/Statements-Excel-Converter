"""Ground-truth assertions for the Regions sample (synthetic fixture
regions_checking_2022-01.pdf — all values are fabricated placeholders)."""
from datetime import date
from decimal import Decimal

from statement_parsers import regions
from statement_parsers.base import TX_DEPOSIT, TX_INTEREST, TX_WITHDRAWAL


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
