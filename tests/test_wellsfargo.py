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
