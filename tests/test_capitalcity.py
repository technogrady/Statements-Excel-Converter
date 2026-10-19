"""Ground-truth assertions for the Capital City Bank sample (synthetic
fixture capitalcity_checking_2021-12.pdf — all values are fabricated
placeholders), plus the machine-read repairs the scanned originals need."""
from datetime import date
from decimal import Decimal

import pytest

from statement_parsers import capitalcity
from statement_parsers.base import TX_CHECK, TX_DEPOSIT, TX_WITHDRAWAL

# Statement A: 2021-11-16 → 2021-12-14, the one whose address is redacted.
EXPECTED_A = [
    (date(2021, 11, 20), Decimal("5000.00"), TX_DEPOSIT, None),
    (date(2021, 11, 22), Decimal("-100.00"), TX_WITHDRAWAL, None),
    (date(2021, 11, 30), Decimal("-400.00"), TX_CHECK, "5001"),
    (date(2021, 12, 1), Decimal("500.00"), TX_DEPOSIT, None),
    (date(2021, 12, 2), Decimal("-200.00"), TX_WITHDRAWAL, None),
    (date(2021, 12, 3), Decimal("-500.00"), TX_CHECK, "5002"),
    (date(2021, 12, 8), Decimal("-600.00"), TX_CHECK, "5004"),
    (date(2021, 12, 10), Decimal("-300.00"), TX_WITHDRAWAL, None),
]

# Statement B: 2021-12-15 → 2022-01-14, the period that spans the year end.
EXPECTED_B = [
    (date(2021, 12, 20), Decimal("2000.00"), TX_DEPOSIT, None),
    (date(2021, 12, 28), Decimal("-50.00"), TX_WITHDRAWAL, None),
    (date(2022, 1, 5), Decimal("-150.00"), TX_WITHDRAWAL, None),
    (date(2022, 1, 10), Decimal("-800.00"), TX_CHECK, "5005"),
]


def _rows(statement):
    return sorted(
        (t.date, t.amount, t.tx_type, t.check_no) for t in statement.transactions
    )


class TestCapitalCityGroundTruth:
    def test_one_pdf_yields_every_statement_period_it_holds(self, capitalcity_result):
        periods = [(s.period_start, s.period_end) for s in capitalcity_result.statements]
        assert periods == [
            (date(2021, 11, 16), date(2021, 12, 14)),
            (date(2021, 12, 15), date(2022, 1, 14)),
        ]

    def test_identity(self, capitalcity_result):
        for statement in capitalcity_result.statements:
            assert statement.bank == "Capital City Bank"
            assert statement.account_last4 == "9012"
            assert statement.account_number_full is None  # the number is masked
            assert statement.account_label == "EVERYDAY CHECKING FOR BUSINESS"

    def test_balances_chain_between_the_two_statements(self, capitalcity_result):
        first, second = capitalcity_result.statements
        assert first.opening_balance == Decimal("10000.00")
        assert first.closing_balance == Decimal("13400.00")
        assert second.opening_balance == first.closing_balance
        assert second.closing_balance == Decimal("14400.00")

    def test_first_statement_transactions(self, capitalcity_result):
        assert _rows(capitalcity_result.statements[0]) == EXPECTED_A

    def test_second_statement_transactions(self, capitalcity_result):
        assert _rows(capitalcity_result.statements[1]) == EXPECTED_B

    def test_both_reconcile_with_nothing_to_report(self, capitalcity_result):
        for statement in capitalcity_result.statements:
            assert statement.reconciled, statement.notes
            assert statement.notes == []

    def test_dec_to_jan_period_splits_the_year(self, capitalcity_result):
        second = capitalcity_result.statements[1]
        years = {t.date.year for t in second.transactions}
        assert years == {2021, 2022}

    def test_gap_in_check_numbers_is_flagged_on_the_check(self, capitalcity_result):
        checks = {t.check_no: t for t in capitalcity_result.statements[0].transactions
                  if t.tx_type == TX_CHECK}
        assert "gap in check numbers" in checks["5004"].description
        assert "gap" not in checks["5001"].description

    def test_wrapped_description_survives_the_page_break(self, capitalcity_result):
        debit = next(t for t in capitalcity_result.statements[1].transactions
                     if t.amount == Decimal("-50.00"))
        # 'EXAMPLE HARDWARE' ends page 1; 'ANYTOWNFL' resumes under the
        # reprinted header on page 2.
        assert debit.description.endswith("EXAMPLE HARDWARE ANYTOWNFL")

    def test_page_furniture_never_lands_in_a_description(self, capitalcity_result):
        for statement in capitalcity_result.statements:
            for t in statement.transactions:
                assert "Primary Account" not in t.description
                assert "Continued" not in t.description
                assert "Page" not in t.description
                assert "EXAMPLE CONTRACTING" not in t.description
                # the redacted address block, and the real one below it
                assert "eee" not in t.description
                assert "EXAMPLE LN" not in t.description


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_matches_on_the_marketing_blurb(self):
        assert capitalcity.matches("learn more at ccbg.com/makingcents")
        assert capitalcity.matches("your Capital City Bank Visa debit card")

    def test_matches_on_the_layout_when_the_blurb_is_absent(self):
        # The wordmark is an image; a statement printed without the promo
        # carries no bank name at all, so the layout has to carry detection.
        assert capitalcity.matches(
            "Primary Account XXXXXXX9012\nPrevious Balance 10,000.00\n"
            "Ending Balance 13,400.00"
        )

    def test_does_not_claim_a_servisfirst_statement(self):
        # ServisFirst's similar core-banking layout says 'Primary Acct.' and
        # 'Current Balance'; neither half of the fallback may fire on it.
        assert not capitalcity.matches(
            "ServisFirst Bank\nPrimary Acct. XXXXXXXXXXXX5678\n"
            "Previous Balance 20,000.00\nCurrent Balance 26,550.00"
        )


# ---------------------------------------------------------------------------
# Machine-read repairs. The real statements are scans; each repair below is
# admitted only because the statement itself decides the reading.
# ---------------------------------------------------------------------------

def _scan(*body: str) -> list[str]:
    """One machine-read page holding a whole small statement."""
    return ["\n".join([
        "10000000000 002 30",
        "ccbg.com/makingcents. Terms and conditions apply.",
        "EXAMPLE CONTRACTING LLC Date 1/31/22 Page 1",
        "12 EXAMPLE LN Primary Account XXXXXXX9012",
        "ANYTOWN FL 00000-0000",
        "CHECKING ACCOUNT",
        "EVERYDAY CHECKING FOR BUSINESS Images 1",
        "Account Number XXXXXXX9012 Statement Dates 1/01/22 thru 1/31/22",
        "Previous Balance 1,000.00 Days in this Statement Period 31",
        *body,
    ])]


class TestOcrRepairs:
    def test_money_split_at_its_decimal_point_is_rejoined(self):
        # A scan of one real page reads '448.28-' as '448 .28-'. Without the
        # repair the row books as $0.28 and the statement is $448.00 out.
        pages = _scan(
            "1 Deposits/Credits .00 Avg Ledger Balance 900.00",
            "1 Checks/Debits 448.28 Avg Collected Balance 900.00",
            "Ending Balance 551.72",
            "OTHER DEBITS",
            "Date Description Amount",
            "1/16    DBT CRD 1822 01/16/24 26900014            448 .28-",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert statement.reconciled, statement.notes
        assert statement.transactions[0].amount == Decimal("-448.28")

    def test_a_sub_dollar_amount_in_its_own_column_is_left_alone(self):
        # The repair closes a *single*-space gap only. A genuine '.28' sits a
        # whole column away from the description, so it stays $0.28.
        pages = _scan(
            "1 Deposits/Credits .00 Avg Ledger Balance 900.00",
            "1 Checks/Debits .28 Avg Collected Balance 900.00",
            "Ending Balance 999.72",
            "OTHER DEBITS",
            "Date Description Amount",
            "1/16    DBT CRD 1822 01/16/24 26900014                .28-",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert statement.transactions[0].amount == Decimal("-0.28")
        assert statement.reconciled, statement.notes

    def test_stray_digit_in_a_date_is_resolved_against_the_period(self):
        # '7/22' scans as '71/22'. 1/22 and 7/22 are both readings; only one
        # of them is inside a July statement.
        pages = ["\n".join([
            "ccbg.com/makingcents",
            "12 EXAMPLE LN Primary Account XXXXXXX9012",
            "ANYTOWN FL 00000-0000",
            "CHECKING ACCOUNT",
            "EVERYDAY CHECKING FOR BUSINESS Images 1",
            "Account Number XXXXXXX9012 Statement Dates 7/01/24 thru 7/31/24",
            "Previous Balance 1,000.00 Days in this Statement Period 31",
            "1 Checks/Debits 653.35 Avg Collected Balance 900.00",
            "Ending Balance 346.65",
            "OTHER DEBITS",
            "Date Description Amount",
            "71/22 AFT Trsfr to Loan 653.35-",
        ])]
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert statement.transactions[0].date == date(2024, 7, 22)
        assert statement.reconciled
        assert any("71/22" in note for note in statement.notes)

    def test_an_undecidable_date_is_never_quietly_moved(self):
        # In a period that contains both 1/22 and 7/22 there is no single
        # reading, so the printed one stands and the row is reported.
        pages = ["\n".join([
            "ccbg.com/makingcents",
            "12 EXAMPLE LN Primary Account XXXXXXX9012",
            "ANYTOWN FL 00000-0000",
            "CHECKING ACCOUNT",
            "EVERYDAY CHECKING FOR BUSINESS Images 1",
            "Account Number XXXXXXX9012 Statement Dates 1/01/24 thru 12/31/24",
            "Previous Balance 1,000.00 Days in this Statement Period 366",
            "Ending Balance 346.65",
            "OTHER DEBITS",
            "Date Description Amount",
            "71/22 AFT Trsfr to Loan 653.35-",
        ])]
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert not statement.transactions
        assert any("71/22" in note for note in statement.notes)

    def test_mangled_column_rule_is_not_mistaken_for_data(self):
        # One real page reads 'Date Description Amount' as
        # 'Dates Description) Amott'.
        pages = _scan(
            "1 Deposits/Credits 100.00 Avg Ledger Balance 900.00",
            "Ending Balance 1,100.00",
            "DEPOSITS AND OTHER CREDITS",
            "Dates Description) Amott",
            "1/16 Deposit 100.00",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert statement.reconciled, statement.notes
        assert statement.notes == []


class TestRefusals:
    def test_blacked_out_account_number_says_so(self):
        # Statements are grouped by account number; guessing one would
        # silently merge unrelated accounts.
        pages = _scan(
            "Ending Balance 1,000.00",
        )
        pages[0] = pages[0].replace("Account Number XXXXXXX9012",
                                    "Account Number |<>|")
        with pytest.raises(ValueError, match="blacked out"):
            capitalcity.parse(pages, "scan.pdf")

    def test_a_file_with_no_statement_block_says_so(self):
        with pytest.raises(ValueError, match="no statement blocks"):
            capitalcity.parse(["ccbg.com/makingcents\nnothing else here"], "x.pdf")


class TestCrossChecks:
    def test_declared_totals_that_disagree_are_reported(self):
        pages = _scan(
            "2 Deposits/Credits 300.00 Avg Ledger Balance 900.00",
            "Ending Balance 1,100.00",
            "DEPOSITS AND OTHER CREDITS",
            "Date Description Amount",
            "1/16 Deposit 100.00",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert any("declares 2 Deposits/Credits, parsed 1" in n for n in statement.notes)
        assert any("$300.00 declared, parsed $100.00" in n for n in statement.notes)

    def test_daily_balance_table_names_the_day_a_figure_went_wrong(self):
        pages = _scan(
            "1 Deposits/Credits 100.00 Avg Ledger Balance 900.00",
            "Ending Balance 1,100.00",
            "DEPOSITS AND OTHER CREDITS",
            "Date Description Amount",
            "1/16 Deposit 100.00",
            "DAILY BALANCE INFORMATION",
            "Date Balance Date Balance",
            "1/16 1,900.00 1/31 1,100.00",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert statement.reconciled  # the transactions themselves still add up
        assert any("2022-01-16 prints $1,900.00" in n for n in statement.notes)

    def test_nonzero_service_charge_is_reported_not_invented(self):
        pages = _scan(
            "Service Charges 15.00",
            "Ending Balance 985.00",
        )
        statement = capitalcity.parse(pages, "scan.pdf")[0]
        assert not statement.transactions  # nothing is fabricated
        assert any("Service Charges of $15.00" in n for n in statement.notes)
        assert not statement.reconciled  # and it is never passed off as balanced
