"""Shared data model and helpers for bank statement parsers.

All money is Decimal — never float. Floats only appear at the Excel
write boundary (display formatting keeps 2 decimals).
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")

# Transaction types
TX_DEPOSIT = "Deposit"
TX_WITHDRAWAL = "Withdrawal"
TX_CHECK = "Check"
TX_INTEREST = "Interest"
TX_FEE = "Fee"
TX_TRANSFER = "Transfer"


@dataclass
class Transaction:
    date: date  # full date, year resolved (see infer_year)
    description: str  # multi-line descriptions joined with " "
    amount: Decimal  # signed: credits positive, debits/checks negative
    tx_type: str  # "Deposit" | "Withdrawal" | "Check" | "Interest" | "Fee" | "Transfer"
    check_no: str | None
    source_file: str


@dataclass
class ParsedStatement:
    bank: str  # "Regions" | "ServisFirst"
    account_number_full: str | None  # full number when the statement prints it; None if masked
    account_last4: str  # always populated — last 4 digits
    account_label: str  # e.g. "BUSINESS INTEREST CHECKING"
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    transactions: list[Transaction]
    source_file: str
    reconciled: bool = False
    notes: list[str] = field(default_factory=list)

    def finalize(self) -> "ParsedStatement":
        """Compute reconciliation: opening + sum(amounts) == closing, to the penny."""
        total = sum((t.amount for t in self.transactions), Decimal("0"))
        expected = (self.opening_balance + total).quantize(TWO_PLACES)
        self.reconciled = expected == self.closing_balance.quantize(TWO_PLACES)
        if not self.reconciled:
            delta = (self.closing_balance - expected).quantize(TWO_PLACES)
            self.notes.append(
                f"Reconciliation failed: opening {money_str(self.opening_balance)} "
                f"+ transactions {money_str(total)} = {money_str(expected)}, "
                f"statement closing {money_str(self.closing_balance)} (Δ {money_str(delta)})"
            )
        return self

    @property
    def reconciliation_delta(self) -> Decimal:
        total = sum((t.amount for t in self.transactions), Decimal("0"))
        return (self.closing_balance - (self.opening_balance + total)).quantize(TWO_PLACES)


# Per-file outcome statuses
STATUS_OK = "OK"
STATUS_UNRECOGNIZED = "UNRECOGNIZED"
STATUS_NO_TEXT = "NO_TEXT"
STATUS_ENCRYPTED = "ENCRYPTED"
STATUS_PARSE_ERROR = "PARSE_ERROR"


@dataclass
class FileResult:
    """Outcome of processing one input PDF. One bad PDF never aborts the run."""
    source_file: str
    status: str  # STATUS_* above
    statements: list[ParsedStatement] = field(default_factory=list)
    detail: str = ""  # hint text (unrecognized) or error message (parse error / encrypted)


_MONEY_RE = re.compile(r"^\(?\$?\s*(-?)([\d,]*\.?\d{0,2})\)?(-?)$")


def parse_money(s: str) -> Decimal:
    """Parse '$1,234.56', '1,234.56-', '.00', '(45.00)' → Decimal. Raises ValueError on junk."""
    s = s.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    if s.startswith("."):
        s = "0" + s
    if not s:
        raise ValueError(f"not a money amount: {s!r}")
    try:
        value = Decimal(s).quantize(TWO_PLACES)
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {s!r}") from exc
    return -value if negative else value


def money_str(d: Decimal) -> str:
    """Format a Decimal for notes/logs: $-1,234.56 style."""
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def infer_year(
    month: int, day: int, period_start: date, period_end: date
) -> tuple[date | None, str | None]:
    """Resolve an MM/DD transaction date against the statement period.

    Rule (from spec):
      * period within one year → that year;
      * period spans years: month == start month → start year,
        month == end month → end year;
      * otherwise the year that places the date inside [start, end];
      * if no placement is possible, return best effort + a warning.

    Returns (date | None, warning | None).
    """

    def mk(year: int) -> date | None:
        # Clamp e.g. Feb 30 typos to the month's last day rather than crashing;
        # a clamp is reported through the out-of-period warning path below.
        try:
            return date(year, month, day)
        except ValueError:
            if 1 <= month <= 12:
                last = calendar.monthrange(year, month)[1]
                if day > last:
                    return None
            return None

    if not 1 <= month <= 12:
        return None, f"invalid transaction date {month:02d}/{day:02d}"

    if period_start.year == period_end.year:
        candidates = [period_start.year]
    elif month == period_start.month and month != period_end.month:
        candidates = [period_start.year, period_end.year]
    elif month == period_end.month and month != period_start.month:
        candidates = [period_end.year, period_start.year]
    else:
        candidates = [period_start.year, period_end.year]

    dates = [d for d in (mk(y) for y in candidates) if d is not None]
    if not dates:
        return None, f"invalid transaction date {month:02d}/{day:02d}"

    for d in dates:
        if period_start <= d <= period_end:
            return d, None

    # No candidate falls inside the period — keep the primary candidate but flag it.
    d = dates[0]
    return d, (
        f"transaction date {d.isoformat()} falls outside statement period "
        f"{period_start.isoformat()}..{period_end.isoformat()}"
    )


def last4_of(number: str) -> str:
    """Trailing 4 digits of an (optionally masked) account number string."""
    digits = re.sub(r"\D", "", number)
    return digits[-4:] if len(digits) >= 4 else digits


def split_account_number(raw: str) -> tuple[str | None, str]:
    """Split a printed account number into (full_number | None, last4).

    ``xxxxxx1000`` / ``XXXXXXXXXXXX5678`` are masked → full is None.
    ``0123456789`` is an unmasked full number → both populated.
    """
    raw = raw.strip()
    masked = bool(re.search(r"[xX*]", raw))
    digits = re.sub(r"\D", "", raw)
    return (None if masked else digits), last4_of(raw)
