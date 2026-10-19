"""Capital City Bank statement parser.

Calibrated against the extracted text of Capital City Bank business
checking statements (see tests/fixtures/extracted_text/). The layout is
the fixed-pitch, column-ruled core-banking print Capital City issues:

* every page opens with a control line (``10000000000 002 30``), an
  optional marketing blurb, ``<customer name> Date 1/31/24 Page 1`` and
  ``<address> Primary Account XXXXXXX9012`` — all page furniture, cut
  before parsing so a page break can never glue itself into a wrapped
  description;
* **one PDF routinely holds a year of statements, back to back.** Each
  begins at a ``CHECKING ACCOUNT`` banner and ends at ``END OF
  STATEMENT``; the parser emits one ParsedStatement per period, and
  consolidation chains their balances;
* the product label sits on its own line with a right-hand column
  (``EVERYDAY CHECKING FOR BUSINESS Images 23``) and repeats on
  continuation pages as ``<label> XXXXXXX9012 (Continued)``;
* summary lines interleave a right-hand column too (``Previous Balance
  10,282.45 Days in this Statement Period 31``), so every regex anchors
  on the left-column label and never on end-of-line. The account number
  is masked (``XXXXXXX9012``) — only the last four digits are available;
* ``DEPOSITS AND OTHER CREDITS`` rows are ``M/DD description amount``;
  ``OTHER DEBITS`` amounts carry a trailing minus (``67.00-``). Both
  sections wrap long descriptions onto following lines (merchant name,
  then city/state) and continue across pages under a repeated header;
* ``CHECKS IN NUMBER ORDER`` packs two ``date check-no[*] amount``
  triplets per row, positive amounts that are debits; ``*`` marks a gap
  in the check numbers, not an out-of-sequence serial (the statement
  says so itself: ``* Denotes missing check numbers``);
* ``DAILY BALANCE INFORMATION`` is not a transaction table, but it is
  the strongest check available on a machine-read scan: the balance it
  prints for each day must equal the opening balance plus every
  transaction up to that day. Mismatches are reported by date, so a
  misread digit points at the day it came from.

Capital City prints ``Service Charges`` and ``Interest Paid`` as their
own summary lines, outside the Deposits/Credits and Checks/Debits
totals. Neither is turned into a transaction — a nonzero one that no
parsed row accounts for is reported instead, and reconciliation
(base.finalize) fails rather than importing an invented row.

Scanned statements (``--ocr``) reach this parser as machine-read text.
The misreads it repairs are only those the statement itself decides:
a decimal point read as a comma or colon (``67,00``) is unambiguous
because a thousands separator always has three digits after it, and
``.00`` read as ``-00`` can only be cents, since this layout never
prints a bare amount without them.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from .base import (
    TX_CHECK,
    TX_DEPOSIT,
    TX_FEE,
    TX_INTEREST,
    TX_TRANSFER,
    TX_WITHDRAWAL,
    ParsedStatement,
    Transaction,
    infer_year,
    money_str,
    parse_money,
    split_account_number,
)

BANK = "Capital City Bank"

# The wordmark is an image on these statements; the only text that names the
# bank is the marketing blurb on page 1 of each statement.
_SIGNATURES = re.compile(r"capital\s*city\s*bank|ccbg\.com", re.IGNORECASE)
# ...which a statement printed without the blurb would not carry at all, so
# the layout itself is the fallback. 'Primary Account' (spelled out) alongside
# 'Ending Balance' is unique to Capital City here: ServisFirst's otherwise
# similar core-banking layout prints 'Primary Acct.' and 'Current Balance'.
_PRIMARY_ACCOUNT_RE = re.compile(r"\bPrimary Account\s+[Xx*]*\d", re.IGNORECASE)
_ENDING_BALANCE_RE = re.compile(r"^\s*Ending Balance\s+[\d,.]", re.IGNORECASE | re.MULTILINE)


def matches(text: str) -> bool:
    text = text or ""
    if _SIGNATURES.search(text):
        return True
    return bool(_PRIMARY_ACCOUNT_RE.search(text) and _ENDING_BALANCE_RE.search(text))


# ---------------------------------------------------------------------------
# Money, with the two misreads a scan of this layout actually produces.
# ---------------------------------------------------------------------------

# A scan reads the decimal point as a comma or a colon ('67,00'). The reading
# is never ambiguous: a thousands separator is always followed by three
# digits, so a separator with exactly two digits after it is a decimal point.
_CENTS = r"[.,:]\d{2}(?!\d)"
_DECIMAL_MISREAD_RE = re.compile(r"[,:](\d{2})(?!\d)")
# Amounts under a dollar print as '.00' / '.45'; a scan reads that leading
# point as a dash. A bare '-45' cannot be a negative amount in this layout —
# every printed figure carries its cents — so it can only be '.45'.
_BARE_CENTS_RE = re.compile(r"^-(\d{2})$")

# A scan sometimes splits a money token at its decimal point ('448 .28'), and
# the gap is always exactly one space — the printed gap between the
# description and the right-aligned amount column is many. Closing a
# single-space gap therefore can't swallow a description: the only reading it
# changes is one whose amount would otherwise have no dollars at all.
_SPLIT_MONEY_RE = re.compile(r"(?<=\d) (?=[.,:]\d{2}(?!\d))")


def _money(token: str) -> Decimal:
    """parse_money, after restoring a decimal point the scan misread."""
    token = token.strip()
    bare = _BARE_CENTS_RE.match(token)
    if bare:
        token = "." + bare.group(1)
    return parse_money(_DECIMAL_MISREAD_RE.sub(r".\1", token))


_AMOUNT = rf"[\d,]*{_CENTS}|-\d{{2}}"

# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------

# '<address> Primary Account XXXXXXX9012' — the last line of the repeated page
# header. Everything above it on the page is furniture.
_HEADER_END_RE = re.compile(r"\bPrimary Account\s+([Xx*]*\d+)", re.IGNORECASE)
# The mailing address continues below it; this line ends the block.
_CITY_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(-\d{4})?\s*$")
# Fallback furniture, for a page whose header line didn't extract cleanly.
_FURNITURE_RES = [
    re.compile(r"^\d{8,}\s+\d{2,3}\s+\d{1,3}$"),            # control line
    re.compile(r"\bDate\s+\d{1,2}/\d{1,2}/\d{2,4}\s+Page\s+\d+\s*$", re.IGNORECASE),
    re.compile(r"\bPrimary Account\b", re.IGNORECASE),
    re.compile(r"ccbg\.com|Making Cents|Terms and conditions apply", re.IGNORECASE),
]
# Markers that begin real content — the address scan stops here so it can
# never swallow a banner, a summary line or a transaction row.
_CONTENT_START_RES = [
    re.compile(r"^\d{1,2}/\d{1,2}\s"),
    re.compile(r"^(Account Number|Previous Balance|Ending Balance|Service Charges"
               r"|Interest Paid)\b", re.IGNORECASE),
    re.compile(r"^\d+\s+(Deposits/Credits|Checks/Debits)\b", re.IGNORECASE),
    re.compile(r"\(Continued\)\s*$", re.IGNORECASE),
    re.compile(r"^(CHECKING ACCOUNT|DEPOSITS AND OTHER CREDITS|OTHER DEBITS"
               r"|CHECKS IN NUMBER ORDER|DAILY BALANCE INFORMATION)\b", re.IGNORECASE),
]


def _is_content_start(line: str) -> bool:
    return any(rx.search(line) for rx in _CONTENT_START_RES)


def _clean_pages(pages: list[str]) -> list[str]:
    """Strip the repeated per-page header, then flatten to one line stream so
    sections that span pages (even mid-transaction) parse seamlessly.

    Runs of whitespace are collapsed: nothing here reads column positions, and
    OCR pads columns with long space runs, so normalizing makes machine-read
    and pdfplumber text the same shape.
    """
    out: list[str] = []
    for page in pages:
        lines = [
            re.sub(r"[ \t]+", " ", _SPLIT_MONEY_RE.sub("", ln)).strip()
            for ln in (page or "").splitlines()
        ]
        lines = [ln for ln in lines if ln]
        cut = 0
        for i, line in enumerate(lines[:10]):
            if _HEADER_END_RE.search(line):
                cut = i + 1
                break
        if cut:
            # The mailing address continues under the header line. Drop it, up
            # to and including its city/ST/ZIP, but stop at real content.
            j = cut
            while j < len(lines) and j < cut + 4:
                if _is_content_start(lines[j]):
                    break
                if _CITY_STATE_ZIP_RE.search(lines[j]):
                    cut = j + 1
                    break
                j += 1
        for line in lines[cut:]:
            if any(rx.search(line) for rx in _FURNITURE_RES):
                continue
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Statement anatomy
# ---------------------------------------------------------------------------

_SEC_DEPOSITS = "DEPOSITS"
_SEC_DEBITS = "DEBITS"
_SEC_CHECKS = "CHECKS"
_SEC_BALANCES = "BALANCES"

_BANNER_RE = re.compile(r"^CHECKING ACCOUNT\s*$", re.IGNORECASE)
_LABEL_RE = re.compile(r"^(?P<label>.+?)\s+Images\s+\d+\s*$", re.IGNORECASE)
_CONTINUED_RE = re.compile(r"\(Continued\)\s*$", re.IGNORECASE)
_CONTINUED_LABEL_RE = re.compile(r"^(?P<label>.+?)\s+[Xx*]*\d+\s+\(Continued\)\s*$",
                                 re.IGNORECASE)
_ACCOUNT_NUMBER_RE = re.compile(r"^Account Number\s+(?P<number>\S+)", re.IGNORECASE)
_STMT_DATES_RE = re.compile(
    r"Statement Dates?\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+thru\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_PREV_BAL_RE = re.compile(rf"^Previous Balance\s+({_AMOUNT})(-?)", re.IGNORECASE)
_END_BAL_RE = re.compile(rf"^Ending Balance\s+({_AMOUNT})(-?)", re.IGNORECASE)
_DEPOSITS_DECL_RE = re.compile(rf"^(\d+)\s+Deposits/Credits\s+({_AMOUNT})", re.IGNORECASE)
_DEBITS_DECL_RE = re.compile(rf"^(\d+)\s+Checks/Debits\s+({_AMOUNT})", re.IGNORECASE)
_SERVICE_CHARGE_RE = re.compile(rf"^Service Charges?\s+({_AMOUNT})", re.IGNORECASE)
_INTEREST_PAID_RE = re.compile(rf"^Interest Paid\s+({_AMOUNT})", re.IGNORECASE)
_END_OF_STATEMENT_RE = re.compile(r"-*END OF STATEMENT-*\s*$", re.IGNORECASE)

_SECTION_HEADERS = [
    (re.compile(r"^DEPOSITS AND OTHER CREDITS\b", re.IGNORECASE), _SEC_DEPOSITS),
    (re.compile(r"^OTHER DEBITS\b", re.IGNORECASE), _SEC_DEBITS),
    (re.compile(r"^CHECKS IN NUMBER ORDER\b", re.IGNORECASE), _SEC_CHECKS),
    (re.compile(r"^DAILY BALANCE INFORMATION\b", re.IGNORECASE), _SEC_BALANCES),
]

# Every table's column rule begins with the word 'Date' and is drawn with an
# underscore leader ('Amount___ Date'), which a scan mangles freely — one real
# page reads 'Dates Description) Amott'. Matching the word alone is enough and
# can't eat data: a table row always *begins* with a date, never with the word
# (and \b keeps a description like 'Dateline' out).
_COLHDR_RE = re.compile(r"^[\W_]*Dates?\b", re.IGNORECASE)
_MISSING_CHECKS_FOOTNOTE_RE = re.compile(r"^\*?\s*Denotes missing check numbers",
                                         re.IGNORECASE)

_TX_RE = re.compile(rf"^(\d{{1,2}}/\d{{1,2}})\s+(.+?)\s+({_AMOUNT})(-?)$")
_CHECK_TRIPLET_RE = re.compile(rf"(\d{{1,2}}/\d{{1,2}})\s+(\d+)(\*?)\s+({_AMOUNT})")
_BALANCE_PAIR_RE = re.compile(rf"(\d{{1,2}}/\d{{1,2}})\s+({_AMOUNT})(-?)")


def _parse_period_date(s: str):
    fmt = "%m/%d/%Y" if len(s.split("/")[-1]) == 4 else "%m/%d/%y"
    return datetime.strptime(s, fmt).date()


def _infer(month_s: str, day_s: str, period_start, period_end):
    """infer_year on string parts, rejecting out-of-range numbers up front."""
    if not (month_s.isdigit() and day_s.isdigit()):
        return None, f"unreadable transaction date {month_s}/{day_s}"
    month, day = int(month_s), int(day_s)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None, f"invalid transaction date {month_s}/{day_s}"
    return infer_year(month, day, period_start, period_end)


def _digit_deletions(month_s: str, day_s: str):
    """Every reading of the date with one stray digit removed."""
    for i in range(len(month_s)):
        if len(month_s) > 1:
            yield month_s[:i] + month_s[i + 1:], day_s
    for i in range(len(day_s)):
        if len(day_s) > 1:
            yield month_s, day_s[:i] + day_s[i + 1:]


def _tx_type(description: str, amount: Decimal, check_no: str | None) -> str:
    if check_no:
        return TX_CHECK
    d = description.lower()
    if "interest" in d:
        return TX_INTEREST
    if "service charge" in d or re.search(r"\bfee\b", d):
        return TX_FEE
    if "transfer" in d:
        return TX_TRANSFER
    return TX_DEPOSIT if amount > 0 else TX_WITHDRAWAL


class _Statement:
    """One statement period's worth of accumulated state."""

    def __init__(self, label: str, account_raw: str, filename: str):
        self.label = label
        self.account_full, self.last4 = split_account_number(account_raw)
        self.filename = filename
        self.opening: Decimal | None = None
        self.closing: Decimal | None = None
        self.period_start = None
        self.period_end = None
        self.declared_credits: tuple[int, Decimal] | None = None
        self.declared_debits: tuple[int, Decimal] | None = None
        self.service_charges: Decimal | None = None
        self.interest_paid: Decimal | None = None
        self.transactions: list[Transaction] = []
        self.daily_balances: list[tuple[object, Decimal]] = []
        self.notes: list[str] = []

    def resolve(self, token: str):
        """Resolve an 'M/DD' table cell to a real date, repairing the one
        mangle a scan of this layout produces: a speckle read as an extra
        digit ('7/22' → '71/22').

        The statement period is the arbiter, never likelihood. A repair is
        taken only when deleting one digit leaves exactly one reading that
        lands inside the period — '71/22' can be read as 7/22 or 1/22, and in
        a July statement only the first is a date. Anything less decisive
        keeps the printed reading and its warning, so a row is never quietly
        moved to a day it didn't happen on.
        """
        if self.period_start is None or self.period_end is None:
            raise ValueError(
                f"{BANK}: dated row seen before Statement Dates for account x{self.last4}"
            )
        month_s, _, day_s = token.partition("/")
        resolved, warning = _infer(month_s, day_s, self.period_start, self.period_end)
        if warning is None:
            return resolved

        repairs = {
            fixed
            for candidate_m, candidate_d in _digit_deletions(month_s, day_s)
            for fixed, note in [_infer(candidate_m, candidate_d,
                                       self.period_start, self.period_end)]
            if note is None
        }
        if len(repairs) == 1:
            repaired = repairs.pop()
            self.notes.append(
                f"{BANK}: machine-read date {token!r} read as "
                f"{repaired.isoformat()} — the only reading inside the "
                "statement period; check it against the PDF"
            )
            return repaired

        self.notes.append(warning)
        return resolved

    def to_statement(self) -> ParsedStatement:
        if self.opening is None or self.closing is None:
            raise ValueError(
                f"{BANK}: Previous/Ending Balance not found for account x{self.last4}"
            )
        if self.period_start is None or self.period_end is None:
            raise ValueError(
                f"{BANK}: Statement Dates not found for account x{self.last4}"
            )
        self._cross_check()
        return ParsedStatement(
            bank=BANK,
            account_number_full=self.account_full,
            account_last4=self.last4,
            account_label=self.label,
            period_start=self.period_start,
            period_end=self.period_end,
            opening_balance=self.opening,
            closing_balance=self.closing,
            transactions=self.transactions,
            source_file=self.filename,
            notes=self.notes,
        ).finalize()

    # -- cross-checks ------------------------------------------------------
    # None of these decide whether a statement is imported; reconciliation
    # does. They say *where* a discrepancy came from.

    def _cross_check(self) -> None:
        credits = [t for t in self.transactions if t.amount > 0]
        debits = [t for t in self.transactions if t.amount <= 0]
        if self.declared_credits is not None:
            count, total = self.declared_credits
            got = sum((t.amount for t in credits), Decimal("0"))
            if len(credits) != count:
                self.notes.append(
                    f"{BANK}: statement declares {count} Deposits/Credits, "
                    f"parsed {len(credits)}"
                )
            if got != total:
                self.notes.append(
                    f"{BANK}: Deposits/Credits total {money_str(total)} declared, "
                    f"parsed {money_str(got)}"
                )
        if self.declared_debits is not None:
            count, total = self.declared_debits
            got = -sum((t.amount for t in debits), Decimal("0"))
            if len(debits) != count:
                self.notes.append(
                    f"{BANK}: statement declares {count} Checks/Debits, "
                    f"parsed {len(debits)}"
                )
            if got != total:
                self.notes.append(
                    f"{BANK}: Checks/Debits total {money_str(total)} declared, "
                    f"parsed {money_str(got)}"
                )
        # Service charges and interest sit outside those totals. Neither is
        # invented as a transaction: say so, and let reconciliation fail.
        for amount, name in ((self.service_charges, "Service Charges"),
                             (self.interest_paid, "Interest Paid")):
            if amount and amount != Decimal("0"):
                self.notes.append(
                    f"{BANK}: statement declares {name} of {money_str(amount)}, "
                    "which is not itemized in any transaction section — check "
                    "whether it is already included in a row above"
                )
        self._check_daily_balances()

    def _check_daily_balances(self) -> None:
        """Each printed daily balance must equal opening + everything up to
        that day. On a machine-read scan this is what names the day a misread
        digit came from."""
        if not self.daily_balances or self.opening is None:
            return
        ordered = sorted(self.transactions, key=lambda t: t.date)
        mismatches = []
        for day, printed in self.daily_balances:
            expected = self.opening + sum(
                (t.amount for t in ordered if t.date <= day), Decimal("0")
            )
            if expected != printed:
                mismatches.append((day, printed, expected))
        if not mismatches:
            return
        shown = "; ".join(
            f"{day.isoformat()} prints {money_str(printed)}, "
            f"transactions give {money_str(expected)}"
            for day, printed, expected in mismatches[:3]
        )
        more = "" if len(mismatches) <= 3 else f" (+{len(mismatches) - 3} more)"
        self.notes.append(
            f"{BANK}: daily balance table disagrees with the transactions — {shown}{more}"
        )


def _clean_label(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def parse(pages: list[str], filename: str) -> list[ParsedStatement]:
    lines = _clean_pages(pages)

    statements: list[ParsedStatement] = []
    current: _Statement | None = None
    section: str | None = None
    last_tx: Transaction | None = None
    pending_label = ""
    unreadable_account = False

    def close():
        nonlocal current, section, last_tx
        if current is not None:
            statements.append(current.to_statement())
        current, section, last_tx = None, None, None

    for line in lines:
        if _END_OF_STATEMENT_RE.search(line):
            close()
            continue
        if _BANNER_RE.match(line):
            # A new statement's first page. Close the previous one even if its
            # END OF STATEMENT line didn't survive extraction.
            close()
            continue

        label_match = _LABEL_RE.match(line)
        if label_match and not _ACCOUNT_NUMBER_RE.match(line):
            pending_label = _clean_label(label_match.group("label"))
            last_tx = None
            continue

        account_match = _ACCOUNT_NUMBER_RE.match(line)
        if account_match:
            raw = account_match.group("number")
            if not re.search(r"\d", raw):
                # A blacked-out account number. Accounts are grouped by it, so
                # guessing would silently merge unrelated statements.
                unreadable_account = True
                continue
            close()
            current = _Statement(pending_label, raw, filename)
            dates = _STMT_DATES_RE.search(line)
            if dates:
                current.period_start = _parse_period_date(dates.group(1))
                current.period_end = _parse_period_date(dates.group(2))
            continue

        # '<label> XXXXXXX9012 (Continued)' heads every continuation page. It
        # is dropped, but never resets the open transaction: a description
        # that wrapped across the page break resumes under it.
        if _CONTINUED_RE.search(line):
            continued = _CONTINUED_LABEL_RE.match(line)
            if continued and not pending_label:
                pending_label = _clean_label(continued.group("label"))
            continue

        if current is None:
            continue  # preamble before the first statement in the file

        # ---- summary block ------------------------------------------------
        dates = _STMT_DATES_RE.search(line)
        if dates and current.period_start is None:
            current.period_start = _parse_period_date(dates.group(1))
            current.period_end = _parse_period_date(dates.group(2))
            continue
        match = _PREV_BAL_RE.match(line)
        if match:
            current.opening = _money(match.group(1) + match.group(2))
            continue
        match = _END_BAL_RE.match(line)
        if match:
            current.closing = _money(match.group(1) + match.group(2))
            continue
        match = _DEPOSITS_DECL_RE.match(line)
        if match:
            current.declared_credits = (int(match.group(1)), _money(match.group(2)))
            continue
        match = _DEBITS_DECL_RE.match(line)
        if match:
            current.declared_debits = (int(match.group(1)), _money(match.group(2)))
            continue
        match = _SERVICE_CHARGE_RE.match(line)
        if match:
            current.service_charges = _money(match.group(1))
            continue
        match = _INTEREST_PAID_RE.match(line)
        if match:
            current.interest_paid = _money(match.group(1))
            continue

        # ---- section + column headers --------------------------------------
        header = next((name for rx, name in _SECTION_HEADERS if rx.match(line)), None)
        if header is not None:
            # A section continuing on the next page reprints its header; don't
            # break the wrapped description that straddles the page boundary.
            if header != section:
                section, last_tx = header, None
            continue
        if _COLHDR_RE.match(line) or _MISSING_CHECKS_FOOTNOTE_RE.match(line):
            continue

        if section is None:
            continue

        # ---- table rows ------------------------------------------------------
        if section == _SEC_CHECKS:
            found = False
            for triplet in _CHECK_TRIPLET_RE.finditer(line):
                found = True
                when = current.resolve(triplet.group(1))
                if when is None:
                    continue
                check_no = triplet.group(2)
                description = f"Check {check_no}"
                if triplet.group(3):
                    description += " (gap in check numbers)"
                current.transactions.append(
                    Transaction(when, description, -_money(triplet.group(4)),
                                TX_CHECK, check_no, filename)
                )
            if not found:
                current.notes.append(
                    f"{BANK}: unparsed line in CHECKS IN NUMBER ORDER: {line!r}"
                )
            continue

        if section == _SEC_BALANCES:
            found = False
            for pair in _BALANCE_PAIR_RE.finditer(line):
                found = True
                when = current.resolve(pair.group(1))
                if when is None:
                    continue
                current.daily_balances.append(
                    (when, _money(pair.group(2) + pair.group(3)))
                )
            if not found:
                current.notes.append(
                    f"{BANK}: unparsed line in DAILY BALANCE INFORMATION: {line!r}"
                )
            continue

        match = _TX_RE.match(line)
        if match:
            when = current.resolve(match.group(1))
            if when is None:
                last_tx = None
                continue
            amount = _money(match.group(3))
            if match.group(4) == "-" or section == _SEC_DEBITS:
                # Debit amounts print a trailing minus; honor the section sign
                # if a variant layout drops it.
                amount = -abs(amount)
            description = match.group(2).strip()
            last_tx = Transaction(when, description, amount,
                                  _tx_type(description, amount, None), None, filename)
            current.transactions.append(last_tx)
            continue

        # Wrapped description continuation (merchant name, then city/state).
        if last_tx is not None:
            last_tx.description += " " + line
        else:
            current.notes.append(
                f"{BANK}: stray line in {section} section ignored: {line!r}"
            )

    close()
    if unreadable_account:
        blocked = (
            f"{BANK}: the account number is blacked out — statements are "
            "grouped by account, so it cannot be imported without one. "
            "Use an unredacted copy, or the original download."
        )
        if not statements:
            raise ValueError(blocked)
        # The rest of the file read fine. Keep it, but never let the skipped
        # period pass unmentioned: it would look like a missing month.
        statements[-1].notes.append(
            f"{blocked} One statement period in this file was skipped for that reason."
        )
    if not statements:
        raise ValueError(f"{BANK}: no statement blocks found")
    return statements
