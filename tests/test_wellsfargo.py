"""Wells Fargo parser tests.

The interesting property here is sign recovery: Wells Fargo's extracted
text does not say which money column an amount came from, so credits and
debits are told apart by the ending-daily-balance column, with the
description only breaking ties. Both fixtures deliberately include rows
whose wording points the wrong way.
"""
from datetime import date
from decimal import Decimal

import pytest

from statement_parsers import detect_bank, parse_pdf, wellsfargo
from statement_parsers.base import (
    STATUS_OK,
    STATUS_PARSE_ERROR,
    TX_CHECK,
    TX_DEPOSIT,
    TX_INTEREST,
    TX_TRANSFER,
    TX_WITHDRAWAL,
)


def D(s):
    return Decimal(s)


class TestDetection:
    def test_wordmark(self):
        assert detect_bank("WELLS FARGO Bank, N.A.") is wellsfargo

    def test_website(self):
        assert detect_bank("Online: wellsfargo.com/biz") is wellsfargo

    def test_phone(self):
        assert detect_bank("1-800-CALL-WELLS (1-800-225-5935)") is wellsfargo

    def test_wins_over_servisfirst_generic_footer(self):
        # ServisFirst's last-resort signature is a generic disclosure line; a
        # Wells Fargo statement that happened to carry it must still route here.
        text = "Wells Fargo Bank, N.A.\nNOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION"
        assert detect_bank(text) is wellsfargo

    def test_does_not_hijack_other_banks(self):
        assert detect_bank("Regions Bank") is not wellsfargo
        assert detect_bank("ServisFirst Bank") is not wellsfargo


class TestSavingsStatement:
    def test_header(self, wellsfargo_savings_stmt):
        s = wellsfargo_savings_stmt
        assert s.bank == "Wells Fargo"
        assert s.account_label == "Business Market Rate Savings"
        assert s.account_last4 == "1234"
        assert s.account_number_full == "0000001234"
        assert s.period_start == date(2022, 1, 1)
        assert s.period_end == date(2022, 1, 31)
        assert s.opening_balance == D("1000.00")
        assert s.closing_balance == D("5500.16")

    def test_reconciles(self, wellsfargo_savings_stmt):
        assert wellsfargo_savings_stmt.reconciled, wellsfargo_savings_stmt.notes
        assert wellsfargo_savings_stmt.notes == []

    def test_all_transactions(self, wellsfargo_savings_stmt):
        got = [(t.date, t.amount) for t in wellsfargo_savings_stmt.transactions]
        assert got == [
            (date(2022, 1, 5), D("2000.00")),
            (date(2022, 1, 10), D("1000.00")),
            (date(2022, 1, 10), D("3000.00")),
            (date(2022, 1, 18), D("-500.00")),
            (date(2022, 1, 22), D("-700.00")),
            (date(2022, 1, 22), D("-1300.00")),
            (date(2022, 1, 28), D("1000.00")),
            (date(2022, 1, 31), D("0.16")),
        ]

    def test_wrapped_description_is_joined(self, wellsfargo_savings_stmt):
        first = wellsfargo_savings_stmt.transactions[0]
        assert first.description == (
            "Online Transfer From Example Company Inc Ref #Ab0000001 Business "
            "Checking Quarterly Sales Tax"
        )

    def test_date_shaped_continuation_line_is_not_a_transaction(
        self, wellsfargo_savings_stmt
    ):
        # The '01/22/22' overflow line must join its row, not become one.
        card_tx = wellsfargo_savings_stmt.transactions[4]
        assert card_tx.description.endswith("on 01/22/22")

    def test_sign_recovered_against_the_description_hint(self, wellsfargo_savings_stmt):
        # The 1/28 wire row has no directional wording ('Wt Seq... Srf# ...'),
        # so the hint guesses debit; only the balance column proves it's a credit.
        wire = wellsfargo_savings_stmt.transactions[6]
        assert wellsfargo._guess_sign(wire.description) == -1
        assert wire.amount == D("1000.00")

    def test_types(self, wellsfargo_savings_stmt):
        types = [t.tx_type for t in wellsfargo_savings_stmt.transactions]
        assert types[:6] == [TX_TRANSFER] * 6
        assert types[7] == TX_INTEREST

    def test_declared_totals_agree(self, wellsfargo_savings_stmt):
        credits = sum(t.amount for t in wellsfargo_savings_stmt.transactions if t.amount > 0)
        debits = -sum(t.amount for t in wellsfargo_savings_stmt.transactions if t.amount < 0)
        assert credits == D("7000.16")
        assert debits == D("2500.00")

    def test_disclosure_pages_do_not_leak_in(self, wellsfargo_savings_stmt):
        blob = " ".join(t.description for t in wellsfargo_savings_stmt.transactions)
        assert "IMPORTANT ACCOUNT INFORMATION" not in blob
        assert "worksheet" not in blob.lower()
        assert "service fee" not in blob.lower()


class TestCheckingStatement:
    def test_header(self, wellsfargo_checking_stmt):
        s = wellsfargo_checking_stmt
        assert s.account_label == "Business Choice Checking"
        assert s.account_last4 == "5678"
        assert s.period_start == date(2022, 2, 1)
        assert s.period_end == date(2022, 2, 28)

    def test_reconciles(self, wellsfargo_checking_stmt):
        assert wellsfargo_checking_stmt.reconciled, wellsfargo_checking_stmt.notes
        assert wellsfargo_checking_stmt.notes == []

    def test_check_number_column(self, wellsfargo_checking_stmt):
        check = wellsfargo_checking_stmt.transactions[1]
        assert check.check_no == "1001"
        assert check.tx_type == TX_CHECK
        assert check.amount == D("-1000.00")
        assert check.description == "Check"

    def test_amounts(self, wellsfargo_checking_stmt):
        got = [(t.date, t.amount, t.tx_type) for t in wellsfargo_checking_stmt.transactions]
        assert got == [
            (date(2022, 2, 3), D("3000.00"), TX_DEPOSIT),
            (date(2022, 2, 9), D("-1000.00"), TX_CHECK),
            (date(2022, 2, 14), D("-250.00"), TX_WITHDRAWAL),
            (date(2022, 2, 25), D("1000.00"), TX_DEPOSIT),
        ]

    def test_ach_credit_recovered_despite_debit_hint(self, wellsfargo_checking_stmt):
        ach = wellsfargo_checking_stmt.transactions[3]
        assert wellsfargo._guess_sign(ach.description) == -1  # hint says debit
        assert ach.amount == D("1000.00")  # arithmetic says credit


class TestSignSolver:
    def test_prefers_the_hinted_assignment_when_several_fit(self):
        # +100 -100 and -100 +100 both net to 0; the hint decides.
        signs = wellsfargo._solve_signs([D("100"), D("100")], [1, -1], D("0"))
        assert signs == [1, -1]

    def test_returns_none_when_unsolvable(self):
        assert wellsfargo._solve_signs([D("100")], [1], D("7")) is None

    def test_flips_the_minimum_number_of_rows(self):
        signs = wellsfargo._solve_signs(
            [D("10"), D("20"), D("30")], [-1, -1, -1], D("40")
        )
        # -10 + 20 + 30 = 40 flips one row; no single-sign set is closer.
        assert signs == [-1, 1, 1]


_HEADER_VARIANTS = [
    "January 31, 2024 ■ Page 1 of 3",
    "January 31, 2024 - Page 1 of 3",
    "January 31, 2024 Page 1 of 3",
]


@pytest.mark.parametrize("line", _HEADER_VARIANTS)
def test_page_header_separator_is_tolerated(line):
    m = wellsfargo._STMT_DATE_RE.match(line)
    assert m and m.group(1) == "January 31, 2024"


class TestUnsolvableSignsAreReported:
    """A run the balance column can't explain keeps the hinted signs, records a
    note, and fails reconciliation — never a silent wrong-direction import."""

    def test_note_and_failed_reconciliation(self):
        pages = [
            "Business Market Rate Savings\n"
            "January 31, 2022 Page 1 of 1\n"
            "Online: wellsfargo.com/biz\n"
            "Statement period activity summary Account number: 0000001234\n"
            "Beginning balance on 1/1 $1,000.00\n"
            "Ending balance on 1/31 $1,234.00\n"
            "Transaction history\n"
            "Date Description Credits Debits balance\n"
            "1/05 Some Payment 500.00 1,234.00\n"
            "Totals $0.00 $500.00\n"
        ]
        stmt = wellsfargo.parse(pages, "odd.pdf")[0]
        assert not stmt.reconciled
        assert any("could not resolve credit/debit signs" in n for n in stmt.notes)


class TestCombinedStatementRejected:
    def test_two_summary_blocks(self):
        pages = [
            "Business Choice Checking\n"
            "January 31, 2022 Page 1 of 1\n"
            "wellsfargo.com/biz\n"
            "Statement period activity summary Account number: 0000001234\n"
            "Beginning balance on 1/1 $1,000.00\n"
            "Ending balance on 1/31 $1,000.00\n"
            "Business Market Rate Savings\n"
            "Statement period activity summary Account number: 0000009999\n"
            "Beginning balance on 1/1 $2,000.00\n"
            "Ending balance on 1/31 $2,000.00\n"
        ]
        with pytest.raises(ValueError, match="more than one account"):
            wellsfargo.parse(pages, "combined.pdf")

    def test_router_reports_it_per_file_without_aborting(self, tmp_path):
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        path = tmp_path / "combined.pdf"
        c = canvas.Canvas(str(path), pagesize=letter)
        c.setFont("Courier", 9)
        y = 700
        for line in [
            "Business Choice Checking",
            "January 31, 2022 Page 1 of 1",
            "Online: wellsfargo.com/biz",
            "Statement period activity summary Account number: 0000001234",
            "Beginning balance on 1/1 $1,000.00",
            "Ending balance on 1/31 $1,000.00",
            "Statement period activity summary Account number: 0000009999",
        ]:
            c.drawString(54, y, line)
            y -= 13
        c.showPage()
        c.save()

        r = parse_pdf(path)
        assert r.status == STATUS_PARSE_ERROR
        assert "more than one account" in r.detail


def test_end_to_end_files_parse(fixture_dir):
    for name in ("wellsfargo_savings_2022-01.pdf", "wellsfargo_checking_2022-02.pdf"):
        r = parse_pdf(fixture_dir / name)
        assert r.status == STATUS_OK, r.detail
        assert r.statements[0].reconciled, r.statements[0].notes


class TestOcrShapedInput:
    """OCR output pads columns with long space runs and reads the WELLS FARGO
    wordmark as text onto the title line. Both are normalized away."""

    _PAGES = [
        "Business Market Rate Savings                        WELLS\n"
        "January 31, 2022 @ Page 1 of 2                            FARGO\n"
        "Online: wellsfargo.com/biz\n"
        "Statement period activity summary        Account number: 0000001234\n"
        "Beginning balance on 1/1              $1,000.00      EXAMPLE COMPANY INC\n"
        "Deposits/Credits                       2,000.00\n"
        "Withdrawals/Debits           -   500.00\n"
        "Ending balance on 1/31               $2,500.00\n",
        "January 31, 2022 m Page 2 of 2\n"
        "Transaction history\n"
        "Deposits/          Withdrawals/          Ending daily\n"
        "Date       Description                       Credits      Debits    balance\n"
        "1/05       Online Transfer From Example Inc Ref #Ab01    2,000.00    3,000.00\n"
        "1/18       Online Transfer to Example Inc Ref #Ab02          500.00  2,500.00\n"
        "Ending balance on 1/31                                            2,500.00\n"
        "ES EE TRIES SES ORES EE Te\n"
        "Totals                                    $2,000.00   $500.00\n",
    ]

    def test_parses_and_reconciles(self):
        stmt = wellsfargo.parse(self._PAGES, "scan.pdf")[0]
        assert stmt.reconciled, stmt.notes
        assert stmt.notes == []
        assert [t.amount for t in stmt.transactions] == [D("2000.00"), D("-500.00")]

    def test_wordmark_is_trimmed_from_the_label(self):
        stmt = wellsfargo.parse(self._PAGES, "scan.pdf")[0]
        assert stmt.account_label == "Business Market Rate Savings"

    def test_description_whitespace_is_collapsed(self):
        stmt = wellsfargo.parse(self._PAGES, "scan.pdf")[0]
        assert stmt.transactions[0].description == (
            "Online Transfer From Example Inc Ref #Ab01"
        )

    def test_scan_speckle_line_is_not_a_transaction(self):
        stmt = wellsfargo.parse(self._PAGES, "scan.pdf")[0]
        blob = " ".join(t.description for t in stmt.transactions)
        assert "TRIES" not in blob

    def test_redacted_account_number_is_refused_by_name(self):
        """A blacked-out account number OCRs to punctuation. Grouping depends on
        that number, so the statement is refused with a message naming it."""
        pages = [self._PAGES[0].replace("Account number: 0000001234",
                                        "Account number: |<"),
                 self._PAGES[1]]
        with pytest.raises(ValueError, match="account number not found"):
            wellsfargo.parse(pages, "redacted.pdf")


class TestScanMangledHeader:
    """A scan mangles the page header two ways at once: it drops the space
    after the comma and leaves speckle in the left margin. The header date is
    the period end, so nothing parses at all until both are tolerated."""

    def _pages(self, first, second):
        return [
            f"- Business Market Rate Savings                       WELLS\n"
            f"{first}\n"
            "Online: wellsfargo.com/biz\n"
            "Statement period activity summary   Account number: 0000001234\n"
            "Beginning balance on 1/1            $1,000.00   EXAMPLE COMPANY INC\n"
            "Ending balance on 1/31              $1,000.00\n",
            f"{second}\n"
            "Transaction history\n"
            "Date       Description          Credits    Debits    balance\n"
            "Totals                              $0.00     $0.00\n",
        ]

    def test_missing_space_after_the_comma(self):
        stmt = wellsfargo.parse(
            self._pages("January 31,2022 @ Page 1 of 2", "January 31,2022 m Page 2 of 2"),
            "scan.pdf",
        )[0]
        assert stmt.period_end == date(2022, 1, 31)

    def test_margin_speckle_before_the_date(self):
        stmt = wellsfargo.parse(
            self._pages(". January 31, 2022 ml Page 1 of 2",
                        ". January 31,2022 m@ Page 2 of 2"),
            "scan.pdf",
        )[0]
        assert stmt.period_end == date(2022, 1, 31)

    def test_speckle_is_not_part_of_the_product_title(self):
        stmt = wellsfargo.parse(
            self._pages("January 31,2022 @ Page 1 of 2", "January 31,2022 m Page 2 of 2"),
            "scan.pdf",
        )[0]
        assert stmt.account_label == "Business Market Rate Savings"

    def test_repeated_header_is_still_skipped_inside_the_table(self):
        """The mangled header repeats on every page, including mid-table. It
        must not be glued onto the previous transaction's description."""
        pages = self._pages("January 31,2022 @ Page 1 of 2",
                            ". January 31,2022 m@ Page 2 of 2")
        pages[1] = pages[1].replace(
            "Totals                              $0.00     $0.00\n",
            "1/05    Deposit                    100.00    1,100.00\n"
            ". January 31,2022 m@ Page 2 of 2\n"
            "1/06    Deposit                    100.00    1,200.00\n"
            "Totals                            $200.00       $0.00\n",
        )
        stmt = wellsfargo.parse(pages, "scan.pdf")[0]
        assert [t.description for t in stmt.transactions] == ["Deposit", "Deposit"]


class TestClosedAccountFinalStatement:
    """The last statement of a closed account labels its final figure
    'Closing balance on 11/20' rather than 'Ending balance on 11/30'."""

    _PAGES = [
        "Business Market Rate Savings                          WELLS\n"
        "November 30, 2025 @ Page 1 of 2                        FARGO\n"
        "Online: wellsfargo.com/biz\n"
        "Final Statement for this closed account\n"
        "Statement period activity summary   Account number: 0000001234\n"
        "Beginning balance on 11/1                 $1.65   EXAMPLE COMPANY INC\n"
        "Deposits/Credits                         525.00\n"
        "Withdrawals/Debits              -        526.65\n"
        "Closing balance on 11/20                  $0.00\n",
        "November 30, 2025 m Page 2 of 2\n"
        "Transaction history\n"
        "Date     Description             Credits    Debits   balance\n"
        "11/14    Online Transfer From Example Inc   525.00    526.65\n"
        "11/20    Online Transfer to Example Inc     526.65      0.00\n"
        "Closing balance on 11/20                              0.00\n"
        "Totals                            $525.00  $526.65\n",
    ]

    def test_closing_balance_is_the_closing_balance(self):
        stmt = wellsfargo.parse(self._PAGES, "closed.pdf")[0]
        assert stmt.closing_balance == D("0.00")
        assert stmt.reconciled, stmt.notes

    def test_closing_balance_row_is_not_a_transaction(self):
        stmt = wellsfargo.parse(self._PAGES, "closed.pdf")[0]
        assert len(stmt.transactions) == 2


class TestScanMangledTransactionDate:
    """The slash in a transaction date is a thin mark: a scan drops it ('1/7'
    → '17') or reads it as a digit ('9/15' → '9715'). The statement period
    decides which reading is a date, and an unreadable row is dropped, not
    guessed at."""

    def _pages(self, row):
        return [
            "Business Market Rate Savings                     WELLS\n"
            "September 30, 2022 @ Page 1 of 2                  FARGO\n"
            "Online: wellsfargo.com/biz\n"
            "Statement period activity summary  Account number: 0000001234\n"
            "Beginning balance on 9/1         $1,000.00  EXAMPLE COMPANY INC\n"
            "Ending balance on 9/30           $1,025.00\n",
            "September 30, 2022 m Page 2 of 2\n"
            "Transaction history\n"
            "Date    Description               Credits   Debits   balance\n"
            f"{row}\n"
            "Totals                             $25.00    $0.00\n",
        ]

    def test_dropped_slash_is_restored(self):
        stmt = wellsfargo.parse(
            self._pages("915   Recurring Transfer From Example Inc  25.00  1,025.00"),
            "scan.pdf",
        )[0]
        assert stmt.reconciled, stmt.notes
        assert stmt.transactions[0].date == date(2022, 9, 15)

    def test_slash_read_as_a_digit_is_restored(self):
        stmt = wellsfargo.parse(
            self._pages("9715  Recurring Transfer From Example Inc  25.00  1,025.00"),
            "scan.pdf",
        )[0]
        assert stmt.reconciled, stmt.notes
        assert stmt.transactions[0].date == date(2022, 9, 15)

    def test_the_repair_is_flagged_for_spot_checking(self):
        stmt = wellsfargo.parse(
            self._pages("9715  Recurring Transfer From Example Inc  25.00  1,025.00"),
            "scan.pdf",
        )[0]
        assert any("mangled the slash" in n for n in stmt.notes)

    def test_a_reading_outside_the_period_is_not_a_date(self):
        """'715' could be 7/15, but this is a September statement — so it is
        no date at all, and the row is dropped rather than misfiled."""
        stmt = wellsfargo.parse(
            self._pages("715   Recurring Transfer From Example Inc  25.00  1,025.00"),
            "scan.pdf",
        )[0]
        assert stmt.transactions == []
        assert any("no date in this statement period" in n for n in stmt.notes)
        assert not stmt.reconciled  # the hole is reported, never papered over

    def test_a_wrapped_description_is_not_promoted_to_a_row(self):
        stmt = wellsfargo.parse(
            self._pages(
                "9/15  Recurring Transfer From Example Inc  25.00  1,025.00\n"
                "7408 continued reference line"
            ),
            "scan.pdf",
        )[0]
        assert len(stmt.transactions) == 1
        assert stmt.transactions[0].description.endswith("7408 continued reference line")


class TestScanMangledDecimalPoint:
    """A scan reads the decimal point as a comma or a colon — '3,18', '157:52'.
    The reading is decidable: a thousands separator always has three digits
    after it, so two digits mean a decimal point."""

    def _pages(self, *rows):
        return [
            "Business Market Rate Savings                   WELLS\n"
            "April 30, 2023 @ Page 1 of 2                    FARGO\n"
            "Online: wellsfargo.com/biz\n"
            "Statement period activity summary Account number: 0000001234\n"
            "Beginning balance on 4/1          $128.18  EXAMPLE COMPANY INC\n"
            "Ending balance on 4/30              $3.19\n",
            "April 30, 2023 m Page 2 of 2\n"
            "Transaction history\n"
            "Date    Description             Credits  Debits  balance\n"
            + "".join(row + "\n" for row in rows)
            + "Totals                          $25.01  $150.00\n",
        ]

    def test_comma_for_point_in_amount_and_balance(self):
        stmt = wellsfargo.parse(
            self._pages(
                "4/17  Recurring Transfer From Example Inc   25,00   153,18",
                "4/27  Online Transfer to Example Inc       150,00     3,18",
                "4/28  Interest Payment                       0,01     3.19",
            ),
            "scan.pdf",
        )[0]
        assert stmt.reconciled, stmt.notes
        assert [t.amount for t in stmt.transactions] == [
            D("25.00"), D("-150.00"), D("0.01")
        ]

    def test_colon_for_point(self):
        stmt = wellsfargo.parse(
            self._pages(
                "4/17  Recurring Transfer From Example Inc   25.00   153:18",
                "4/27  Online Transfer to Example Inc       150.00     3.18",
                "4/28  Interest Payment                       0.01     3.19",
            ),
            "scan.pdf",
        )[0]
        assert stmt.reconciled, stmt.notes

    def test_a_real_thousands_separator_is_left_alone(self):
        assert wellsfargo._money("1,234.56") == D("1234.56")
        assert wellsfargo._money("25,00") == D("25.00")

    def test_a_bare_thousands_group_is_not_read_as_cents(self):
        """'Ref 1,234' must not be mistaken for a $1.23 amount."""
        assert wellsfargo._TRAIL_MONEY_RE.search(" Ref 1,234") is None
        assert wellsfargo._TRAIL_MONEY_RE.search(" 3,18") is not None
