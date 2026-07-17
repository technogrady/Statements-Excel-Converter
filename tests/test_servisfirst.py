"""Ground-truth assertions for the ServisFirst sample (synthetic fixture
servisfirst_checking_2022-09.pdf — all values are fabricated placeholders)."""
from datetime import date
from decimal import Decimal

from statement_parsers.base import TX_CHECK, TX_DEPOSIT, TX_WITHDRAWAL

EXPECTED_DEPOSITS = [  # date-ordered
    (date(2022, 7, 30), Decimal("1000.00")),
    (date(2022, 8, 6), Decimal("2000.00")),
    (date(2022, 8, 8), Decimal("3000.00")),   # remote deposit
    (date(2022, 8, 12), Decimal("4000.00")),
    (date(2022, 8, 15), Decimal("500.00")),    # remote deposit
    (date(2022, 8, 19), Decimal("1500.00")),
    (date(2022, 8, 22), Decimal("2500.00")),
    (date(2022, 8, 27), Decimal("3500.00")),
    (date(2022, 8, 29), Decimal("1000.00")),
]

EXPECTED_DEBITS = [  # the 9 non-check debit rows, date-ordered
    Decimal("-1000.00"),   # ACH ACME SUPPLY
    Decimal("-50.00"),     # DBT CRD
    Decimal("-200.00"),    # EXAMPLE UTILITY
    Decimal("-300.00"),    # TRANSFER TO LOAN
    Decimal("-400.00"),    # ACH ACME SUPPLY
    Decimal("-25.00"),     # DBT CRD
    Decimal("-500.00"),    # ACH EXAMPLE CARD
    Decimal("-100.00"),    # EXAMPLE POWER CO
    Decimal("-75.00"),     # UTILITY DIRECT DEBIT
]

EXPECTED_CHECKS = {
    "5001": (date(2022, 8, 2), Decimal("-100.00")),
    "5002": (date(2022, 7, 31), Decimal("-200.00")),
    "5003": (date(2022, 7, 31), Decimal("-300.00")),
    "5004": (date(2022, 8, 5), Decimal("-400.00")),
    "5005": (date(2022, 8, 7), Decimal("-500.00")),
    "5006": (date(2022, 8, 9), Decimal("-600.00")),
    "5008": (date(2022, 8, 16), Decimal("-800.00")),
    "5009": (date(2022, 8, 20), Decimal("-900.00")),
    "5010": (date(2022, 8, 12), Decimal("-1000.00")),
    "5011": (date(2022, 8, 14), Decimal("-1100.00")),
    "5012": (date(2022, 8, 23), Decimal("-1200.00")),
    "5013": (date(2022, 8, 26), Decimal("-1300.00")),
    "5014": (date(2022, 8, 28), Decimal("-1400.00")),
}


class TestServisFirstGroundTruth:
    def test_identity(self, servisfirst_stmt):
        s = servisfirst_stmt
        assert s.bank == "ServisFirst"
        assert s.account_last4 == "5678"
        assert s.account_number_full is None  # ServisFirst masks the number
        assert s.account_label == "BUSINESS CHECKING"

    def test_period_two_digit_years(self, servisfirst_stmt):
        assert servisfirst_stmt.period_start == date(2022, 7, 30)
        assert servisfirst_stmt.period_end == date(2022, 9, 2)

    def test_balances(self, servisfirst_stmt):
        assert servisfirst_stmt.opening_balance == Decimal("20000.00")
        assert servisfirst_stmt.closing_balance == Decimal("26550.00")

    def test_nine_deposits_totaling_ground_truth(self, servisfirst_stmt):
        deposits = sorted(
            ((t.date, t.amount) for t in servisfirst_stmt.transactions
             if t.tx_type == TX_DEPOSIT)
        )
        assert deposits == sorted(EXPECTED_DEPOSITS)
        assert sum(a for _, a in deposits) == Decimal("19000.00")

    def test_nine_noncheck_debit_rows(self, servisfirst_stmt):
        debits = [t for t in servisfirst_stmt.transactions if t.tx_type == TX_WITHDRAWAL]
        assert [t.amount for t in debits] == EXPECTED_DEBITS

    def test_thirteen_checks_no_5007(self, servisfirst_stmt):
        checks = {t.check_no: t for t in servisfirst_stmt.transactions
                  if t.tx_type == TX_CHECK}
        assert len(checks) == 13
        assert "5007" not in checks
        for no, (d, amount) in EXPECTED_CHECKS.items():
            assert checks[no].date == d, no
            assert checks[no].amount == amount, no

    def test_out_of_sequence_flag_on_5010(self, servisfirst_stmt):
        checks = {t.check_no: t for t in servisfirst_stmt.transactions
                  if t.tx_type == TX_CHECK}
        assert "out of sequence" in checks["5010"].description
        assert all("out of sequence" not in t.description
                   for no, t in checks.items() if no != "5010")

    def test_check_dates_inside_period(self, servisfirst_stmt):
        s = servisfirst_stmt
        for t in s.transactions:
            assert s.period_start <= t.date <= s.period_end

    def test_22_checks_debits_totaling_ground_truth(self, servisfirst_stmt):
        debits = [t for t in servisfirst_stmt.transactions if t.amount < 0]
        assert len(debits) == 22
        assert sum(t.amount for t in debits) == Decimal("-12450.00")

    def test_reconciles_exactly(self, servisfirst_stmt):
        s = servisfirst_stmt
        assert s.reconciled is True
        assert (s.opening_balance + Decimal("19000.00") - Decimal("12450.00")
                == s.closing_balance)

    def test_no_crosscheck_mismatch_notes(self, servisfirst_stmt):
        # Declared counts (9 Deposits/Credits, 22 Checks/Debits), declared
        # totals, and check-image captions must all agree with parsed data.
        assert servisfirst_stmt.notes == []

    def test_wrapped_continuation_lines_joined(self, servisfirst_stmt):
        debits = [t for t in servisfirst_stmt.transactions if t.tx_type == TX_WITHDRAWAL]
        by_amount = {t.amount: t for t in debits}
        # stray wrapped account-number fragment stays in the description
        deposits = [t for t in servisfirst_stmt.transactions if t.tx_type == TX_DEPOSIT]
        assert any(t.description.endswith(" 80") for t in deposits)
        # multi-line card transaction: merchant + location wrapped lines
        assert "SAMPLE MERCHANT ANYTOWN ST C#0001" in by_amount[Decimal("-50.00")].description

    def test_page_break_mid_transaction(self, servisfirst_stmt):
        # Page 2 opens with the continuation line 'ANYTOWN ST C#0002'
        # belonging to the last debit on page 1 (the 8/18 DBT CRD).
        debits = [t for t in servisfirst_stmt.transactions if t.tx_type == TX_WITHDRAWAL]
        dbt = next(t for t in debits if t.amount == Decimal("-25.00"))
        assert dbt.description == "DBT CRD 0002 08/17/22 00000000 GENERIC STORE #001 ANYTOWN ST C#0002"

    def test_repeated_mailing_address_not_glued_into_description(self, servisfirst_stmt):
        # The customer mailing block reprints atop every page; it must never
        # leak into a transaction description.
        for t in servisfirst_stmt.transactions:
            assert "EXAMPLE LLC" not in t.description
            assert "123 EXAMPLE ST" not in t.description
            assert "ANYTOWN ST 00000" not in t.description

    def test_check_images_and_daily_balances_not_imported(self, servisfirst_stmt):
        # 9 deposits + 9 debits + 13 checks and nothing else — the check-image
        # captions (pages 3-4) and DAILY BALANCES table must not import.
        assert len(servisfirst_stmt.transactions) == 31


class TestServisFirstMultiAccount:
    def test_two_account_blocks_keep_their_own_labels(self, servisfirst_multi):
        stmts = {s.account_last4: s for s in servisfirst_multi.statements}
        assert set(stmts) == {"1111", "2222"}
        assert stmts["1111"].account_label == "BUSINESS CHECKING"
        assert stmts["2222"].account_label == "BUSINESS SAVINGS"

    def test_each_block_reconciles(self, servisfirst_multi):
        for s in servisfirst_multi.statements:
            assert s.reconciled, s.account_last4

    def test_label_not_glued_into_prior_block(self, servisfirst_multi):
        for s in servisfirst_multi.statements:
            for t in s.transactions:
                assert "SAVINGS" not in t.description
                assert "CHECKING" not in t.description


class TestServisFirstOverdrawn:
    def test_trailing_minus_balance_parses_negative(self, servisfirst_overdrawn):
        s = servisfirst_overdrawn
        assert s.opening_balance == Decimal("500.00")
        assert s.closing_balance == Decimal("-200.00")

    def test_reconciles(self, servisfirst_overdrawn):
        s = servisfirst_overdrawn
        assert s.reconciled is True
        assert s.opening_balance + sum(t.amount for t in s.transactions) == s.closing_balance
