from datetime import date
from decimal import Decimal

import pytest

from statement_parsers.base import (
    infer_year,
    last4_of,
    parse_money,
    split_account_number,
)


class TestParseMoney:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,234.56", Decimal("1234.56")),
            ("$111,222.33", Decimal("111222.33")),
            ("9,999.99-", Decimal("-9999.99")),
            (".00", Decimal("0.00")),
            ("$0.00", Decimal("0.00")),
            ("(45.00)", Decimal("-45.00")),
            ("-12.34", Decimal("-12.34")),
            ("100,000.00", Decimal("100000.00")),
        ],
    )
    def test_values(self, raw, expected):
        got = parse_money(raw)
        assert got == expected
        assert isinstance(got, Decimal)

    def test_rejects_junk(self):
        with pytest.raises(ValueError):
            parse_money("not money")


class TestInferYear:
    PS, PE = date(2021, 12, 15), date(2022, 1, 14)

    def test_same_year_period(self):
        d, warn = infer_year(8, 6, date(2022, 7, 30), date(2022, 9, 2))
        assert d == date(2022, 8, 6) and warn is None

    def test_year_split_december(self):
        d, warn = infer_year(12, 18, self.PS, self.PE)
        assert d == date(2021, 12, 18) and warn is None

    def test_year_split_january(self):
        d, warn = infer_year(1, 3, self.PS, self.PE)
        assert d == date(2022, 1, 3) and warn is None

    def test_period_boundary_dates(self):
        assert infer_year(12, 15, self.PS, self.PE)[0] == date(2021, 12, 15)
        assert infer_year(1, 14, self.PS, self.PE)[0] == date(2022, 1, 14)

    def test_month_outside_period_flags(self):
        d, warn = infer_year(6, 15, self.PS, self.PE)
        assert d is not None
        assert warn is not None and "outside statement period" in warn

    def test_invalid_month_flags(self):
        d, warn = infer_year(13, 1, self.PS, self.PE)
        assert d is None and warn is not None

    def test_feb_29_valid_leap_year(self):
        d, warn = infer_year(2, 29, date(2020, 2, 11), date(2020, 3, 10)); assert d == date(2020, 2, 29) and warn is None


class TestAccountNumbers:
    def test_masked_regions(self):
        full, last4 = split_account_number("xxxxxx1000")
        assert full is None and last4 == "1000"

    def test_masked_servisfirst(self):
        full, last4 = split_account_number("XXXXXXXXXXXX5678")
        assert full is None and last4 == "5678"

    def test_full_number(self):
        full, last4 = split_account_number("0123456789")
        assert full == "0123456789" and last4 == "6789"

    def test_last4_of(self):
        assert last4_of("xxxxxx1000") == "1000"
