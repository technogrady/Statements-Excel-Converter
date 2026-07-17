"""ServisFirst Bank statement parser.

Calibrated against pdfplumber ``page.extract_text()`` output of a
ServisFirst business checking statement (see
tests/fixtures/extracted_text/):

* every page repeats a header block (bank name/address, ``Date 8/30/22
  Page N``, ``Primary Acct. XXXXXXXXXXXX5678``) followed by the customer
  mailing-address block, plus a footer (``MEMBER FDIC`` / ``NOTICE: SEE
  REVERSE SIDE...``) — all stripped before parsing so page breaks can't
  pollute wrapped descriptions;
* account blocks live under a ``C H E C K I N G   A C C O U N T S``
  banner (letters may extract space-separated); the parser loops over
  ``Account Number`` blocks so a statement carrying several accounts in
  one PDF still parses — each block keeps its own product label;
* summary lines interleave a right-hand column (``Previous Balance
  20,000.00 Days in the statement period 35``) — regexes anchor on the
  left-column label; balances may print a trailing minus when overdrawn;
* DEPOSITS AND OTHER CREDITS rows are ``M/DD description amount``;
  WITHDRAWALS AND DEBITS amounts carry a trailing minus (``1,000.00-``);
  wrapped description lines (including stray account-number fragments
  like ``80``) join the previous transaction; sections continue across
  pages and may break mid-transaction;
* the CHECKS table packs 1–3 ``date  check-no[*]  amount`` triplets per
  row; ``*`` = serial number out of sequence;
* DAILY BALANCES and the check-image caption pages (``Check 5001
  Amount $100.00 Date 8/2/2022``) are not transactions — captions are
  only used to cross-verify the CHECKS table.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from .base import (
    TX_CHECK,
    TX_DEPOSIT,
    TX_WITHDRAWAL,
    ParsedStatement,
    Transaction,
    infer_year,
    money_str,
    parse_money,
    split_account_number,
)

BANK = "ServisFirst"

_SIGNATURES = re.compile(r"servisfirst|servis1st", re.IGNORECASE)


def matches(text: str) -> bool:
    return bool(_SIGNATURES.search(text or ""))


_SEC_DEPOSITS = "DEPOSITS"
_SEC_WITHDRAWALS = "WITHDRAWALS"
_SEC_CHECKS = "CHECKS"
_SEC_SKIP = "SKIP"

_ACCOUNT_NUMBER_RE = re.compile(r"^Account Number\s+([Xx*]*\d+)\b")
# Balances may print a trailing minus when overdrawn (e.g. '200.00-'); capture
# it so the sign survives (parse_money handles the trailing '-').
_PREV_BAL_RE = re.compile(r"^Previous Balance\s+([\d,]*\.\d{2}-?)")
_CURR_BAL_RE = re.compile(r"^Current Balance\s+([\d,]*\.\d{2}-?)")
_DEPOSITS_DECL_RE = re.compile(r"^(\d+)\s+Deposits/Credits\s+([\d,]*\.\d{2})")
_DEBITS_DECL_RE = re.compile(r"^(\d+)\s+Checks/Debits\s+([\d,]*\.\d{2})-?")
_STMT_DATES_RE = re.compile(
    r"Statement Dates?\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+thru\s+(\d{1,2}/\d{1,2}/\d{2,4})"
)
_TX_RE = re.compile(r"^(\d{1,2}/\d{1,2})\s+(.+?)\s+([\d,]*\.\d{2})(-?)$")
_CHECK_TRIPLET_RE = re.compile(r"(\d{1,2}/\d{1,2})\s+(\d+)(\*?)\s+([\d,]+\.\d{2})")
_CAPTION_RE = re.compile(
    r"^(?:Check\s+(\d+)\s+)?Amount\s+\$([\d,]+\.\d{2})\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})\s*$"
)
_COLHDR_TX_RE = re.compile(r"^Date\s+Description\s+Amount$", re.IGNORECASE)
_COLHDR_CHECKS_RE = re.compile(r"^(Date\s+Check\s+No\.?\s+Amount\s*)+$", re.IGNORECASE)
_COLHDR_BAL_RE = re.compile(r"^(Date\s+Balance\s*)+$", re.IGNORECASE)
_OOS_FOOTNOTE_RE = re.compile(r"^\*\s*Indicates Serial Number Out of Sequence", re.IGNORECASE)

_FOOTER_RES = [
    re.compile(r"^MEMBER FDIC\b", re.IGNORECASE),
    re.compile(r"^NOTICE: SEE REVERSE SIDE", re.IGNORECASE),
    re.compile(r"\(Continued\)\s*$", re.IGNORECASE),
]
# Fallback page-header furniture, used when a page has no 'Primary Acct.' line.
_HEADER_RES = [
    re.compile(r"^ServisFirst Bank$", re.IGNORECASE),
    re.compile(r"^Servis1st Bank$", re.IGNORECASE),
    re.compile(r"^Date \d{1,2}/\d{1,2}/\d{2,4}(\s+Page\s+\d+)?$", re.IGNORECASE),
    re.compile(r"^Page \d+$", re.IGNORECASE),
    re.compile(r"^Primary Acct\.?\s"),
    re.compile(r"^\(?\d{3}\)?[ .-]\d{3}[.-]\d{4}$"),  # phone
]


def _banner_line(line: str) -> bool:
    return re.sub(r"\s+", "", line).upper() == "CHECKINGACCOUNTS"


# City / ST / ZIP line that ends the customer mailing-address block.
_CITY_STATE_ZIP_RE = re.compile(r"[A-Z]{2}\s+\d{5}(-\d{4})?$")
# Markers that begin real content — the mailing-block scan must stop here so it
# never swallows a section/account/transaction line.
_CONTENT_START_RES = [
    re.compile(r"^\d{1,2}/\d{1,2}\b"),  # transaction / check-triplet row
    re.compile(r"^Account Number\b"),
    re.compile(r"\(Continued\)\s*$", re.IGNORECASE),
    re.compile(r"^(DEPOSITS AND OTHER CREDITS|WITHDRAWALS AND DEBITS|CHECKS|DAILY BALANCES)\b"),
    re.compile(r"^(Previous|Current) Balance\b"),
]


def _is_content_start(line: str) -> bool:
    return _banner_line(line) or any(rx.search(line) for rx in _CONTENT_START_RES)


def _looks_like_label(line: str) -> bool:
    """A standalone all-caps product label (e.g. 'BUSINESS CHECKING') — no
    digits, not the banner. Used to attach the right label to each account."""
    return (
        bool(re.search(r"[A-Za-z]", line))
        and line == line.upper()
        and not re.search(r"\d", line)
        and not _banner_line(line)
    )


def _clean_pages(pages: list[str]) -> list[str]:
    """Strip repeated per-page furniture, then flatten to one line stream so
    sections that span pages (even mid-transaction) parse seamlessly."""
    out: list[str] = []
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines()]
        # The header block repeats at the top of every page and ends at the
        # 'Primary Acct.' line — drop through it when present near the top.
        cut = 0
        for i, ln in enumerate(lines[:10]):
            if re.match(r"^Primary Acct\.?\s", ln):
                cut = i + 1
                break
        # The customer mailing-address block reprints right below the header on
        # every page. Drop it (up to and including its city/ST/ZIP line) so it
        # can't glue into a wrapped description on a continuation page — but
        # stop at the first real content marker so nothing else is lost.
        if cut:
            j = cut
            while j < len(lines) and j < cut + 5:
                candidate = lines[j].strip()
                if not candidate:
                    j += 1
                    continue
                if _is_content_start(candidate):
                    break
                if _CITY_STATE_ZIP_RE.search(candidate):
                    cut = j + 1
                    break
                j += 1
        body = lines[cut:] if cut else lines
        for ln in body:
            if not ln:
                continue
            if any(rx.search(ln) for rx in _FOOTER_RES):
                continue
            if cut == 0 and any(rx.search(ln) for rx in _HEADER_RES):
                continue
            out.append(ln)
    return out


def _parse_period_date(s: str):
    fmt = "%m/%d/%Y" if len(s.split("/")[-1]) == 4 else "%m/%d/%y"
    return datetime.strptime(s, fmt).date()


class _AccountBlock:
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
        self.transactions: list[Transaction] = []
        self.captions: list[tuple[str | None, Decimal]] = []
        self.notes: list[str] = []

    def infer(self, mm: int, dd: int):
        if self.period_start is None or self.period_end is None:
            raise ValueError("ServisFirst: transaction row seen before Statement Dates")
        d, warn = infer_year(mm, dd, self.period_start, self.period_end)
        if warn:
            self.notes.append(warn)
        return d

    def to_statement(self) -> ParsedStatement:
        if self.opening is None or self.closing is None:
            raise ValueError(
                f"ServisFirst: Previous/Current Balance not found for account x{self.last4}"
            )
        if self.period_start is None or self.period_end is None:
            raise ValueError(
                f"ServisFirst: Statement Dates not found for account x{self.last4}"
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

    def _cross_check(self) -> None:
        credits = [t for t in self.transactions if t.amount > 0]
        debits = [t for t in self.transactions if t.amount <= 0]
        if self.declared_credits is not None:
            n, total = self.declared_credits
            got_total = sum((t.amount for t in credits), Decimal("0"))
            if len(credits) != n:
                self.notes.append(
                    f"ServisFirst: statement declares {n} Deposits/Credits, parsed {len(credits)}"
                )
            if got_total != total:
                self.notes.append(
                    f"ServisFirst: Deposits/Credits total {money_str(total)} declared, "
                    f"parsed {money_str(got_total)}"
                )
        if self.declared_debits is not None:
            n, total = self.declared_debits
            got_total = -sum((t.amount for t in debits), Decimal("0"))
            if len(debits) != n:
                self.notes.append(
                    f"ServisFirst: statement declares {n} Checks/Debits, parsed {len(debits)}"
                )
            if got_total != total:
                self.notes.append(
                    f"ServisFirst: Checks/Debits total {money_str(total)} declared, "
                    f"parsed {money_str(got_total)}"
                )
        # Check-image captions duplicate the CHECKS table — never imported,
        # but they cross-verify it (caption dates are check *written* dates,
        # so only the amounts are compared; the table's cleared date rules).
        table = {t.check_no: -t.amount for t in self.transactions if t.tx_type == TX_CHECK}
        for check_no, amount in self.captions:
            if check_no is None:
                continue  # remote-deposit image caption
            if check_no not in table:
                self.notes.append(
                    f"ServisFirst: check image caption for check {check_no} "
                    f"({money_str(amount)}) has no CHECKS table entry"
                )
            elif table[check_no] != amount:
                self.notes.append(
                    f"ServisFirst: check {check_no} image caption {money_str(amount)} "
                    f"!= CHECKS table amount {money_str(table[check_no])}"
                )


def parse(pages: list[str], filename: str) -> list[ParsedStatement]:
    lines = _clean_pages(pages)

    statements: list[ParsedStatement] = []
    account: _AccountBlock | None = None
    section: str | None = None
    last_tx: Transaction | None = None
    last_label = ""
    seen_banner = False

    def close_account():
        nonlocal account
        if account is not None:
            statements.append(account.to_statement())
            account = None

    for i, line in enumerate(lines):
        if _banner_line(line):
            seen_banner = True
            continue

        # A product label sits immediately above its Account Number line. Capture
        # it here so each account block (first AND subsequent, in a multi-account
        # statement) gets its own label, and treat it as a boundary so it can't
        # glue into the previous account's last transaction description.
        if (
            _looks_like_label(line)
            and i + 1 < len(lines)
            and _ACCOUNT_NUMBER_RE.match(lines[i + 1])
        ):
            last_label = line
            last_tx = None
            continue

        m = _ACCOUNT_NUMBER_RE.match(line)
        if m:
            close_account()
            account = _AccountBlock(last_label, m.group(1), filename)
            section = None
            last_tx = None
            dm = _STMT_DATES_RE.search(line)  # often shares the Account Number line
            if dm:
                account.period_start = _parse_period_date(dm.group(1))
                account.period_end = _parse_period_date(dm.group(2))
            continue

        if account is None:
            # Preamble before the first account. The label is normally captured
            # by the look-ahead above; as a fallback (e.g. a statement whose
            # banner didn't extract) remember the most recent all-caps line.
            if _looks_like_label(line):
                last_label = line
            continue

        # ---- summary block of the current account -------------------------
        sm = _STMT_DATES_RE.search(line)
        if sm and account.period_start is None:
            account.period_start = _parse_period_date(sm.group(1))
            account.period_end = _parse_period_date(sm.group(2))
            # fall through: the same line may also carry a left-column label
        pm = _PREV_BAL_RE.match(line)
        if pm:
            account.opening = parse_money(pm.group(1))
            continue
        cm = _CURR_BAL_RE.match(line)
        if cm:
            account.closing = parse_money(cm.group(1))
            continue
        dm = _DEPOSITS_DECL_RE.match(line)
        if dm:
            account.declared_credits = (int(dm.group(1)), parse_money(dm.group(2)))
            continue
        bm = _DEBITS_DECL_RE.match(line)
        if bm:
            account.declared_debits = (int(bm.group(1)), parse_money(bm.group(2)))
            continue

        # ---- section headers ----------------------------------------------
        if line == "DEPOSITS AND OTHER CREDITS":
            if section != _SEC_DEPOSITS:
                section, last_tx = _SEC_DEPOSITS, None
            continue
        if line == "WITHDRAWALS AND DEBITS":
            if section != _SEC_WITHDRAWALS:
                section, last_tx = _SEC_WITHDRAWALS, None
            continue
        if line == "CHECKS":
            section, last_tx = _SEC_CHECKS, None
            continue
        if line == "DAILY BALANCES":
            section, last_tx = _SEC_SKIP, None
            continue
        if _COLHDR_TX_RE.match(line) or _COLHDR_CHECKS_RE.match(line) or _COLHDR_BAL_RE.match(line):
            continue
        if _OOS_FOOTNOTE_RE.match(line):
            continue

        # Check-image captions (page 3+) — cross-verification only.
        capm = _CAPTION_RE.match(line)
        if capm:
            section, last_tx = _SEC_SKIP, None
            account.captions.append((capm.group(1), parse_money(capm.group(2))))
            continue

        if section == _SEC_SKIP or section is None:
            continue

        # ---- transaction rows ----------------------------------------------
        if section == _SEC_CHECKS:
            matched = False
            for cm in _CHECK_TRIPLET_RE.finditer(line):
                matched = True
                mm, dd = (int(x) for x in cm.group(1).split("/"))
                tx_date = account.infer(mm, dd)
                if tx_date is None:
                    continue
                check_no = cm.group(2)
                desc = f"Check {check_no}" + (" (out of sequence)" if cm.group(3) else "")
                account.transactions.append(
                    Transaction(tx_date, desc, -parse_money(cm.group(4)), TX_CHECK,
                                check_no, filename)
                )
            if not matched:
                account.notes.append(f"ServisFirst: unparsed line in CHECKS table: {line!r}")
            continue

        m = _TX_RE.match(line)
        if m:
            mm, dd = (int(x) for x in m.group(1).split("/"))
            tx_date = account.infer(mm, dd)
            if tx_date is None:
                last_tx = None
                continue
            amount = parse_money(m.group(3))
            if m.group(4) == "-":
                amount = -amount
            elif section == _SEC_WITHDRAWALS:
                # Withdrawal amounts print a trailing minus; honor the section
                # sign if a variant layout drops it.
                amount = -amount
            tx_type = TX_DEPOSIT if section == _SEC_DEPOSITS else TX_WITHDRAWAL
            last_tx = Transaction(tx_date, m.group(2).strip(), amount, tx_type, None, filename)
            account.transactions.append(last_tx)
            continue

        # Wrapped description continuation (incl. stray account-number
        # fragments like '80', and lines resuming after a page break).
        if last_tx is not None:
            last_tx.description += " " + line
        else:
            account.notes.append(
                f"ServisFirst: stray line in {section} section ignored: {line!r}"
            )

    close_account()
    if not statements:
        raise ValueError("ServisFirst: no account blocks found")
    return statements
