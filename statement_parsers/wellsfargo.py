"""Wells Fargo statement parser.

Calibrated against the layout of Wells Fargo business deposit-account
statements (Business Market Rate Savings / Business Choice Checking and
friends), as rendered by ``pdfplumber``'s ``page.extract_text()``:

* page 1 carries a product title (``Business Market Rate Savings``)
  above a header line ``January 31, 2024 <sep> Page 1 of 3``; the header
  date is the period *end* date and repeats on every page;
* the ``Statement period activity summary`` block interleaves a
  right-hand column (``Beginning balance on 1/1 $2,764.43  ACME INC``),
  so its regexes anchor on the left-column label and never on
  end-of-line; the period *start* comes from ``Beginning balance on
  M/D`` and the closing balance from ``Ending balance on M/D``;
* ``Transaction history`` rows are ``M/D description [amount] [ending
  daily balance]``, with wrapped description lines following; checking
  layouts insert a ``Number`` (check number) column between the date and
  the description — detected from the column header;
* **the extracted text does not say which money column an amount came
  from.** Deposits/Credits and Withdrawals/Debits are distinguished only
  by horizontal position, which ``extract_text()`` discards. Signs are
  therefore recovered arithmetically: the ending-daily-balance column
  pins the net change of every run of rows, and each run's signs are
  solved for exactly (preferring the assignment closest to what the
  descriptions suggest, e.g. "Transfer From" = credit, "Transfer to" =
  debit). The statement's declared Deposits/Credits and
  Withdrawals/Debits totals are then cross-checked, and reconciliation
  (base.finalize) is the final guard;
* the transaction table ends at ``Totals`` / ``Ending balance on ...`` —
  everything past it (monthly service fee summary, IMPORTANT ACCOUNT
  INFORMATION, the balance-calculation worksheet) is skipped.

Scanned statements (``--ocr``) reach this parser as machine-read text,
and the same handful of glyphs is misread every time. Each repair here
is admitted only because something in the statement decides it, never
because it looks likely:

* the header date loses the space after its comma and picks up margin
  speckle — the shape around it (``Page 1 of 3``) still pins it;
* a decimal point is read as a comma or a colon (``3,18``, ``157:52``) —
  a thousands separator always has three digits after it, so two digits
  can only be cents;
* a transaction date loses its slash (``1/7`` → ``17``) or has it read
  as a digit (``9/15`` → ``9715``) — the statement period decides which
  reading is a date, and a row no reading fits is dropped, not guessed
  at, leaving a hole reconciliation reports.

Scope: one deposit account per PDF. Wells Fargo also issues *combined*
statements carrying several accounts in one file; those are detected and
rejected with a clear message rather than silently half-imported.
"""
from __future__ import annotations

import itertools
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

BANK = "Wells Fargo"

_SIGNATURES = re.compile(
    r"wells\s*fargo|wellsfargo\.com|1-800-CALL-WELLS", re.IGNORECASE
)


def matches(text: str) -> bool:
    return bool(_SIGNATURES.search(text or ""))


# ---------------------------------------------------------------------------
# Header / summary patterns
# ---------------------------------------------------------------------------

# OCR of a scanned page routinely drops the space after the comma
# ('January 31,2023'), so the separator is matched loosely.
_MONTH_DAY_YEAR = r"[A-Z][a-z]{2,8} \d{1,2},\s*\d{4}"
# 'January 31, 2024 <bullet> Page 1 of 3' — the bullet extracts as anything
# (or nothing), so only the date and the page marker are anchored. A scan
# also drops speckle in the left margin ('. January 31, 2023 ml Page 3 of 4'),
# so a short run of non-letters is tolerated ahead of the month name.
_LEAD_NOISE = r"[^A-Za-z]{0,4}"
_STMT_DATE_RE = re.compile(rf"^{_LEAD_NOISE}({_MONTH_DAY_YEAR})\b.*?\bPage \d+ of \d+")
# Some layouts print the period explicitly: 'January 1, 2024 - January 31, 2024'.
_PERIOD_RANGE_RE = re.compile(rf"({_MONTH_DAY_YEAR})\s*(?:-|through|to)\s*({_MONTH_DAY_YEAR})")
_ACCOUNT_RE = re.compile(r"Account number:?\s*([0-9Xx*•●-]{4,})")

# A scan reads the decimal point as a comma or a colon — '3,18', '157:52'. The
# reading is never ambiguous: a thousands separator is always followed by three
# digits, so a separator with exactly two digits after it is a decimal point.
# Money tokens therefore accept any of the three, and _money restores the point
# before the value is read. The (?!\d) guard is what keeps '1,234' out.
_CENTS = r"[.,:]\d{2}(?!\d)"
_DECIMAL_MISREAD_RE = re.compile(r"[,:](\d{2})(?!\d)")


def _money(token: str) -> Decimal:
    """parse_money, after restoring a decimal point the scan misread."""
    return parse_money(_DECIMAL_MISREAD_RE.sub(r".\1", token))


_BEGIN_BAL_RE = re.compile(
    rf"\bBeginning balance on (\d{{1,2}}/\d{{1,2}})\s+\$?\s*(-?\$?[\d,]*{_CENTS}-?)"
)
# The final statement of a closed account labels the same figure
# 'Closing balance on 11/20' instead of 'Ending balance on 11/30'.
_END_BAL_RE = re.compile(
    rf"\b(?:Ending|Closing) balance on (\d{{1,2}}/\d{{1,2}})\s+\$?\s*(-?\$?[\d,]*{_CENTS}-?)"
)
_DEP_TOTAL_RE = re.compile(rf"^Deposits/Credits\s+-?\s*\$?\s*([\d,]*{_CENTS})")
_WD_TOTAL_RE = re.compile(rf"^Withdrawals/Debits\s+-?\s*\$?\s*-?\s*([\d,]*{_CENTS})")
_TOTALS_ROW_RE = re.compile(
    rf"^Totals?\s+\$?([\d,]*{_CENTS})\s+\$?([\d,]*{_CENTS})\s*$"
)
_SUMMARY_HDR_RE = re.compile(r"^Statement period activity summary", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Transaction-table patterns
# ---------------------------------------------------------------------------

_TX_SECTION_RE = re.compile(r"^Transaction history\b", re.IGNORECASE)
# Column headers wrap over two lines and repeat after every page break.
_COLHDR_RES = [
    re.compile(r"^Date\s+(Number\s+)?Description\b", re.IGNORECASE),
    re.compile(r"^(Check\s+)?Deposits/\s+Withdrawals/\s+Ending daily\s*$", re.IGNORECASE),
    re.compile(r"^(Number\s+)?Credits\s+Debits\s+balance\s*$", re.IGNORECASE),
    re.compile(r"^Check\s*$", re.IGNORECASE),
]
# A 'Number' column header means bare integers after the date are check numbers.
_CHECK_COL_RE = re.compile(r"^Date\s+Number\s+Description\b", re.IGNORECASE)

# Lines inside the table that are furniture, not rows or wrapped descriptions.
_SKIP_IN_TABLE_RES = [
    _STMT_DATE_RE,
    _TX_SECTION_RE,
    re.compile(r"^(?:Ending|Closing) balance on \d{1,2}/\d{1,2}\b"),
    re.compile(r"^Beginning balance on \d{1,2}/\d{1,2}\b"),
    re.compile(r"^Wells Fargo\b", re.IGNORECASE),
    re.compile(r"^Account number:", re.IGNORECASE),
    re.compile(r"^\(continued\)", re.IGNORECASE),
]

# Lines that close the transaction table for good.
_TABLE_END_RES = [
    _TOTALS_ROW_RE,
    re.compile(r"^Totals?\s*$", re.IGNORECASE),
    re.compile(r"^The Ending Daily Balance does not reflect", re.IGNORECASE),
    re.compile(r"^(Monthly )?[Ss]ervice fee summary", re.IGNORECASE),
    re.compile(r"^Summary of checks written", re.IGNORECASE),
    re.compile(r"^Overdraft Protection\b", re.IGNORECASE),
    re.compile(r"^IMPORTANT ACCOUNT INFORMATION", re.IGNORECASE),
    re.compile(r"^Important Information You Should Know", re.IGNORECASE),
    re.compile(r"^Account Balance Calculation Worksheet", re.IGNORECASE),
    re.compile(r"^Interest summary\b", re.IGNORECASE),
]

_DATE_PREFIX_RE = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.*)$")
# OCR of a scan mangles the thin slash in a transaction date: it is dropped
# ('1/7' → '17') or read as a digit ('9/15' → '9715', '7/31' → '7731'). Such
# a line is only *considered* a row here; it is accepted in the post-pass
# below, and only if it reads as a date inside the period (_repair_ocr_dates).
_OCR_DATE_PREFIX_RE = re.compile(r"^(\d{2,5})\s+(.*)$")
# Digits a slash is read as, when it is read as something rather than dropped.
_SLASH_LOOKALIKES = "17"
# A trailing money token must be preceded by whitespace, so it can never bite
# into a reference like 'Ref#12.34'.
_TRAIL_MONEY_RE = re.compile(rf"\s\$?(-?[\d,]*{_CENTS})(-?)$")
_LEADING_CHECK_NO_RE = re.compile(r"^(\d{3,8})\s+(.*)$")

# ---------------------------------------------------------------------------
# Credit / debit hints taken from the description. These only *seed* the sign
# solver — the ending-daily-balance arithmetic overrules them.
# ---------------------------------------------------------------------------

_CREDIT_RE = re.compile(
    r"\bdeposit\b|\binterest\s+(payment|earned|paid)\b|\brefund\b|\breversal\b"
    r"|\bcredit\b|\brebate\b|\bcash\s*in\b|\bedeposit\b",
    re.IGNORECASE,
)
_DEBIT_RE = re.compile(
    r"\bwithdrawal\b|\bpurchase\b|\bcheck\b|\bfee\b|\bservice\s+charge\b"
    r"|\bpayment\s+to\b|\bbill\s+pay\b|\batm\b|\bdebit\b|\bcharge\b|\bpaid\b",
    re.IGNORECASE,
)


def _guess_sign(description: str) -> int:
    """+1 (credit) / -1 (debit) hint from the description; debit when unsure.

    Wells Fargo phrases transfers directionally ('Online Transfer From X' vs
    'Online Transfer ... to Business Card X'), which is the strongest hint
    available, so it is checked before the generic keyword lists.
    """
    if re.search(r"\btransfer\s+from\b", description, re.IGNORECASE):
        return 1
    if re.search(r"\btransfer\b.*\bto\b", description, re.IGNORECASE):
        return -1
    if _CREDIT_RE.search(description):
        return 1
    if _DEBIT_RE.search(description):
        return -1
    return -1


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


# Full enumeration is 2**n; beyond this many rows in one run, only assignments
# within _MAX_DEVIATIONS flips of the description hints are considered.
_FULL_ENUM_LIMIT = 16
_MAX_DEVIATIONS = 6


def _solve_signs(
    amounts: list[Decimal], guess: list[int], target: Decimal
) -> list[int] | None:
    """Signs for ``amounts`` whose signed sum equals ``target``.

    Returns the solution that deviates least from ``guess`` (candidates are
    enumerated by deviation count, so the first hit wins), or None when the
    run cannot be reconciled to the target at all.
    """
    n = len(amounts)
    max_flips = n if n <= _FULL_ENUM_LIMIT else min(n, _MAX_DEVIATIONS)
    for k in range(max_flips + 1):
        for flips in itertools.combinations(range(n), k):
            signs = list(guess)
            for i in flips:
                signs[i] = -signs[i]
            if sum((s * a for s, a in zip(signs, amounts)), Decimal("0")) == target:
                return signs
    return None


class _Row:
    """One parsed transaction line, before its sign is known."""

    def __init__(self, month: int | None, day: int | None, description: str,
                 amount: Decimal, balance: Decimal | None, check_no: str | None,
                 ocr_digits: str | None = None):
        self.month = month
        self.day = day
        # Set when the date arrived mangled; see _repair_ocr_dates.
        self.ocr_digits = ocr_digits
        self.description = description
        self.amount = amount  # magnitude; sign resolved later
        self.balance = balance  # ending daily balance, when the row prints one
        self.check_no = check_no
        self.sign = 0


def _parse_row(rest: str, want_check_col: bool) -> tuple[str, Decimal, Decimal | None, str | None] | None:
    """Split the post-date remainder into (description, amount, balance, check_no).

    Wells Fargo prints at most two money columns per row: the transaction
    amount and — only on the last row of each day — the ending daily balance.
    """
    trailing: list[Decimal] = []
    while len(trailing) < 2:
        m = _TRAIL_MONEY_RE.search(rest)
        if not m:
            break
        value = _money(m.group(1) + m.group(2))
        trailing.insert(0, value)
        rest = rest[: m.start()].rstrip()
    if not trailing:
        return None
    if len(trailing) == 2:
        amount, balance = trailing[0], trailing[1]
    else:
        amount, balance = trailing[0], None

    check_no = None
    if want_check_col:
        cm = _LEADING_CHECK_NO_RE.match(rest)
        if cm:
            check_no, rest = cm.group(1), cm.group(2)
    return rest.strip(), abs(amount), balance, check_no


def _parse_header_date(s: str):
    # Restore the space OCR drops after the comma before strptime sees it.
    return datetime.strptime(re.sub(r",\s*", ", ", s.strip()), "%B %d, %Y").date()


def _resolve_period_start(month: int, day: int, period_end):
    """Year for the 'Beginning balance on M/D' day: the latest year that puts
    the start on or before the period end (periods routinely span Dec→Jan)."""
    for year in (period_end.year, period_end.year - 1):
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            continue
        if candidate <= period_end:
            return candidate
    return None


def _clean_pages(pages: list[str]) -> list[str]:
    """Flatten to one non-empty line stream with runs of whitespace collapsed.

    Nothing here reads column positions, and OCR output pads columns with
    long space runs (``Date          Description``), so normalizing makes
    machine-read and pdfplumber text the same shape.
    """
    out: list[str] = []
    for page in pages:
        for line in (page or "").splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                out.append(line)
    return out


# The 'WELLS FARGO' wordmark sits at the top right of every page. It is an
# image in a downloaded PDF, but OCR reads it and it lands on the title line.
_WORDMARK_TAIL_RE = re.compile(r"[\s|]*\b(WELLS\s*FARGO|WELLS|FARGO)\s*$", re.IGNORECASE)
# Speckle a scanner leaves in the left margin, ahead of the printed title.
_LEAD_NOISE_RE = re.compile(r"^[^A-Za-z0-9$]+")


def parse(pages: list[str], filename: str) -> list[ParsedStatement]:
    lines = _clean_pages(pages)

    if sum(1 for ln in lines if _SUMMARY_HDR_RE.match(ln)) > 1:
        raise ValueError(
            "Wells Fargo: combined statement with more than one account is not "
            "supported — split the PDF into one account per file"
        )

    notes: list[str] = []
    label = _find_label(lines, pages)
    period_end = None
    period_start = None
    account_raw: str | None = None
    opening = closing = None
    declared_credits = declared_debits = None

    rows: list[_Row] = []
    in_table = False
    table_done = False
    want_check_col = False
    last_row: _Row | None = None

    for line in lines:
        # ---- header / summary ---------------------------------------------
        if period_end is None:
            m = _STMT_DATE_RE.match(line)
            if m:
                period_end = _parse_header_date(m.group(1))
        rm = _PERIOD_RANGE_RE.search(line)
        if rm and period_start is None:
            period_start = _parse_header_date(rm.group(1))
            period_end = _parse_header_date(rm.group(2))
        if account_raw is None:
            am = _ACCOUNT_RE.search(line)
            if am:
                account_raw = am.group(1)
        elif not in_table:
            am = _ACCOUNT_RE.search(line)
            if am and _differs(am.group(1), account_raw):
                raise ValueError(
                    "Wells Fargo: combined statement with more than one account is not "
                    "supported — split the PDF into one account per file"
                )
        bm = _BEGIN_BAL_RE.search(line)
        if bm and opening is None:
            opening = _money(bm.group(2))
            if period_start is None and period_end is not None:
                mm, dd = (int(x) for x in bm.group(1).split("/"))
                period_start = _resolve_period_start(mm, dd, period_end)
        em = _END_BAL_RE.search(line)
        if em and closing is None:
            closing = _money(em.group(2))
        dm = _DEP_TOTAL_RE.match(line)
        if dm and declared_credits is None:
            declared_credits = _money(dm.group(1))
        wm = _WD_TOTAL_RE.match(line)
        if wm and declared_debits is None:
            declared_debits = _money(wm.group(1))

        # ---- transaction table ---------------------------------------------
        if table_done:
            continue
        if _TX_SECTION_RE.match(line):
            in_table, last_row = True, None
            continue
        if not in_table:
            continue
        if _CHECK_COL_RE.match(line):
            want_check_col = True
            last_row = None
            continue
        if any(rx.match(line) for rx in _COLHDR_RES):
            last_row = None
            continue
        if any(rx.match(line) for rx in _TABLE_END_RES):
            in_table, table_done, last_row = False, True, None
            continue
        if any(rx.match(line) for rx in _SKIP_IN_TABLE_RES):
            last_row = None
            continue

        dpm = _DATE_PREFIX_RE.match(line)
        if dpm:
            parsed = _parse_row(dpm.group(2), want_check_col)
            if parsed is None:
                # A date-led line with no amount is a wrapped description that
                # happens to begin with a date (e.g. '01/22/24' overflow).
                if last_row is not None:
                    last_row.description += " " + line
                continue
            description, amount, balance, check_no = parsed
            mm, dd = (int(x) for x in dpm.group(1).split("/"))
            last_row = _Row(mm, dd, description, amount, balance, check_no)
            rows.append(last_row)
            continue

        # A machine-read row whose date lost its slash ('17 Recurring ...').
        opm = _OCR_DATE_PREFIX_RE.match(line)
        if opm:
            parsed = _parse_row(opm.group(2), want_check_col)
            if parsed is not None:
                description, amount, balance, check_no = parsed
                last_row = _Row(None, None, description, amount, balance,
                                check_no, ocr_digits=opm.group(1))
                rows.append(last_row)
                continue

        if last_row is not None:
            last_row.description += " " + line

    # ---- validation --------------------------------------------------------
    if period_end is None:
        raise ValueError("Wells Fargo: statement date header not found")
    if opening is None or closing is None:
        raise ValueError("Wells Fargo: Beginning/Ending balance not found")
    if period_start is None:
        raise ValueError("Wells Fargo: statement period start not found")
    if account_raw is None:
        raise ValueError("Wells Fargo: account number not found")

    rows = _repair_ocr_dates(rows, period_start, period_end, notes)

    _assign_signs(rows, opening, closing, notes)

    transactions: list[Transaction] = []
    for row in rows:
        tx_date, warn = infer_year(row.month, row.day, period_start, period_end)
        if warn:
            notes.append(warn)
        if tx_date is None:
            notes.append(
                f"Wells Fargo: dropped row with unusable date "
                f"{row.month:02d}/{row.day:02d}: {row.description!r}"
            )
            continue
        amount = row.sign * row.amount
        transactions.append(
            Transaction(
                tx_date,
                row.description,
                amount,
                _tx_type(row.description, amount, row.check_no),
                row.check_no,
                filename,
            )
        )

    _cross_check(transactions, declared_credits, declared_debits, notes)

    account_full, last4 = split_account_number(account_raw)
    return [
        ParsedStatement(
            bank=BANK,
            account_number_full=account_full,
            account_last4=last4,
            account_label=label,
            period_start=period_start,
            period_end=period_end,
            opening_balance=opening,
            closing_balance=closing,
            transactions=transactions,
            source_file=filename,
            notes=notes,
        ).finalize()
    ]


def _split_ocr_date(digits: str):
    """Ways a mangled date could read as (month, day), best first.

    Two mangles are covered: the slash vanished, leaving the digits run
    together, and the slash was read as a digit that is now sitting between
    them. Every reading is offered; the caller keeps only one that lands
    inside the statement period.
    """
    # Slash swallowed: '17' → 1/7, '715' → 7/15, '1231' → 12/31.
    if len(digits) == 2:
        yield int(digits[0]), int(digits[1])
    elif len(digits) == 3:
        # '715' is 7/15 far more often than 71/5, which isn't a month at all.
        yield int(digits[0]), int(digits[1:])
        yield int(digits[:2]), int(digits[2])
    elif len(digits) == 4:
        yield int(digits[:2]), int(digits[2:])
    # Slash read as a digit: '9715' → 9/15, '7731' → 7/31.
    for i, char in enumerate(digits):
        if char not in _SLASH_LOOKALIKES:
            continue
        month, day = digits[:i], digits[i + 1:]
        if 1 <= len(month) <= 2 and 1 <= len(day) <= 2:
            yield int(month), int(day)


def _repair_ocr_dates(rows: list[_Row], period_start, period_end,
                      notes: list[str]) -> list[_Row]:
    """Give mangled machine-read rows their date back, or drop them.

    The statement period is the arbiter: '17' is accepted as 1/7 only because
    1/7 falls inside the period, and a split that lands outside it is not a
    date at all. A row nothing fits is dropped rather than guessed at — it
    leaves a hole the balance arithmetic and reconciliation both report.
    """
    kept: list[_Row] = []
    for row in rows:
        if row.month is not None:
            kept.append(row)
            continue
        for month, day in _split_ocr_date(row.ocr_digits or ""):
            if not 1 <= month <= 12:
                continue
            resolved, warn = infer_year(month, day, period_start, period_end)
            if resolved is not None and warn is None:
                row.month, row.day = month, day
                notes.append(
                    f"Wells Fargo: read the date of {resolved.isoformat()} "
                    f"({row.description[:40]!r}) as {row.ocr_digits!r} — the "
                    f"scan mangled the slash; verify it against the PDF"
                )
                kept.append(row)
                break
        else:
            notes.append(
                f"Wells Fargo: dropped a row whose date read as "
                f"{row.ocr_digits!r}, which is no date in this statement "
                f"period: {row.description[:60]!r}"
            )
    return kept


def _differs(a: str, b: str) -> bool:
    """Two printed account numbers name different accounts.

    Page headers may print the number masked on one page and in full on
    another, so only the visible digits are compared.
    """
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if not da or not db:
        return False
    return da[-4:] != db[-4:]


def _find_label(lines: list[str], pages: list[str]) -> str:
    """The product title, printed directly above the page-1 date header."""
    page1 = _clean_pages(pages[:1])
    for i, line in enumerate(page1[:8]):
        if _STMT_DATE_RE.match(line):
            for candidate in reversed(page1[:i]):
                cleaned = _clean_label(candidate)
                if cleaned and not _SIGNATURES.match(cleaned):
                    return cleaned
            break
    for line in page1:
        if re.search(r"[A-Za-z]", line) and not _STMT_DATE_RE.match(line):
            return _clean_label(line)
    return ""


def _clean_label(line: str) -> str:
    """A product title with the page's wordmark trimmed off its tail.

    Leading marks are trimmed too: a scan puts speckle in the left margin
    ('- Business Market Rate Savings'), which is not part of the title.
    """
    label = _WORDMARK_TAIL_RE.sub("", line).strip()
    label = _LEAD_NOISE_RE.sub("", label).strip()
    return label if re.search(r"[A-Za-z]", label) else ""


def _assign_signs(rows: list[_Row], opening: Decimal, closing: Decimal,
                  notes: list[str]) -> None:
    """Recover credit/debit signs from the ending-daily-balance column.

    The extracted text drops the column geometry that says whether an amount
    was a credit or a debit, but the balance column pins the net change of
    every run of rows it closes. Each run is solved exactly; the description
    hints only break ties. A run that cannot be solved keeps its hinted signs
    and is reported — reconciliation then fails loudly rather than silently
    booking a deposit as a withdrawal.
    """
    balance = opening
    pending: list[_Row] = []

    def close(target: Decimal, anchor: str) -> Decimal | None:
        amounts = [r.amount for r in pending]
        guess = [_guess_sign(r.description) for r in pending]
        signs = _solve_signs(amounts, guess, target)
        if signs is None:
            for row, s in zip(pending, guess):
                row.sign = s
            notes.append(
                f"Wells Fargo: could not resolve credit/debit signs for "
                f"{len(pending)} transaction(s) before {anchor} "
                f"(net change {money_str(target)}); description hints used instead"
            )
            return None
        for row, s in zip(pending, signs):
            row.sign = s
        return target

    for row in rows:
        pending.append(row)
        if row.balance is None:
            continue
        target = (row.balance - balance)
        close(target, f"the ending daily balance {money_str(row.balance)}")
        balance = row.balance
        pending = []

    if pending:
        # Rows after the last printed daily balance: the statement's closing
        # balance is the anchor.
        close(closing - balance, "the statement closing balance")


def _cross_check(transactions: list[Transaction], declared_credits: Decimal | None,
                 declared_debits: Decimal | None, notes: list[str]) -> None:
    """Compare the recovered signs against the summary's declared totals."""
    if declared_credits is not None:
        got = sum((t.amount for t in transactions if t.amount > 0), Decimal("0"))
        if got != declared_credits:
            notes.append(
                f"Wells Fargo: Deposits/Credits total {money_str(declared_credits)} "
                f"declared, parsed {money_str(got)}"
            )
    if declared_debits is not None:
        got = -sum((t.amount for t in transactions if t.amount < 0), Decimal("0"))
        if got != declared_debits:
            notes.append(
                f"Wells Fargo: Withdrawals/Debits total {money_str(declared_debits)} "
                f"declared, parsed {money_str(got)}"
            )
