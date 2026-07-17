"""Regions Bank statement parser.

Calibrated against pdfplumber ``page.extract_text()`` output of Regions
business checking statements (see tests/fixtures/extracted_text/):

* header: ``ACCOUNT # xxxxxx1000`` (masked) or the full number on
  unmasked statements — the number may sit on the same line or the next;
* the all-caps product label sits directly above the period line
  ``December 15, 2021 through January 14, 2022``;
* SUMMARY block lines interleave a right-hand column
  (``Beginning Balance $10,000.00 Minimum Daily Balance $...``), so
  balance regexes anchor on the left-column label at line start;
* transaction sections are standalone all-caps header lines; rows are
  ``MM/DD description amount``; sections end at a ``Total ...`` line or
  the next header; everything from ``DAILY BALANCE SUMMARY`` onward
  (daily balances, balance-your-account worksheet, disclosures) is
  skipped;
* transaction dates carry no year — resolved against the statement
  period (periods routinely span Dec→Jan).
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

BANK = "Regions"

_SIGNATURES = re.compile(r"regions\s+bank|1-800-REGIONS", re.IGNORECASE)


def matches(text: str) -> bool:
    return bool(_SIGNATURES.search(text or ""))


# Section header → (tx_type, sign). Headers are standalone all-caps lines.
SECTIONS: dict[str, tuple[str, int]] = {
    "DEPOSITS & CREDITS": (TX_DEPOSIT, 1),
    "DEPOSITS AND CREDITS": (TX_DEPOSIT, 1),
    "INTEREST": (TX_INTEREST, 1),
    "WITHDRAWALS": (TX_WITHDRAWAL, -1),
    "FEES": (TX_FEE, -1),
    "CHECKS": (TX_CHECK, -1),
    "AUTOMATIC TRANSFERS": (TX_TRANSFER, -1),
}

# Summary-block cross-check label → section header it should agree with.
_SUMMARY_CROSSCHECKS = {
    "Deposits & Credits": "DEPOSITS & CREDITS",
    "Withdrawals": "WITHDRAWALS",
    "Fees": "FEES",
    "Checks": "CHECKS",
    "Net Interest Earned": "INTEREST",
}

_ACCOUNT_RE = re.compile(r"ACCOUNT\s*#\s*:?\s*([xX*]*\d+)")
_ACCOUNT_LABEL_ONLY_RE = re.compile(r"ACCOUNT\s*#\s*:?\s*$")
_ACCOUNT_NUMBER_ONLY_RE = re.compile(r"^([xX*]*\d+)\s*$")
_PERIOD_RE = re.compile(
    r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+through\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})"
)
_TX_RE = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+\$?([\d,]+\.\d{2})$")
_CHECK_TRIPLET_RE = re.compile(r"(\d{2}/\d{2})\s+(\d+)(\*?)\s+\$?([\d,]+\.\d{2})")
_TOTAL_RE = re.compile(r"^Total\s+(.+?)\s+\$?([\d,]*\.\d{2})$", re.IGNORECASE)
_TERMINAL_RE = re.compile(r"^DAILY BALANCE SUMMARY\b")
_CHECKS_COLHDR_RE = re.compile(r"^(Date\s+Check\s+No\.?\s+Amount\s*)+$", re.IGNORECASE)

# Page furniture that must never be glued into a wrapped description.
_NOISE_RES = [
    re.compile(r"^Page \d+ of \d+$", re.IGNORECASE),
    re.compile(r"^ACCOUNT\s*#", re.IGNORECASE),
    re.compile(r"^Cycle\b"),
    _PERIOD_RE,
]


def _summary_amount(line: str, label: str) -> Decimal | None:
    """Left-column summary value: anchor the label at line start so the
    interleaved right-hand column can't be picked up."""
    m = re.match(rf"^{re.escape(label)}\s+\$([\d,]*\.\d{{2}})", line)
    return parse_money(m.group(1)) if m else None


def _find_header(lines: list[str]) -> tuple[str | None, str, object, object]:
    """Return (account_raw, label, period_start, period_end) from page-1 lines."""
    account_raw = None
    for i, line in enumerate(lines):
        m = _ACCOUNT_RE.search(line)
        if m:
            account_raw = m.group(1)
            break
        # 'ACCOUNT #' alone with the number on the following line
        if _ACCOUNT_LABEL_ONLY_RE.search(line) and i + 1 < len(lines):
            m2 = _ACCOUNT_NUMBER_ONLY_RE.match(lines[i + 1].strip())
            if m2:
                account_raw = m2.group(1)
                break

    period_idx = None
    period_start = period_end = None
    for i, line in enumerate(lines):
        m = _PERIOD_RE.search(line)
        if m:
            period_start = datetime.strptime(re.sub(r"\s+", " ", m.group(1)), "%B %d, %Y").date()
            period_end = datetime.strptime(re.sub(r"\s+", " ", m.group(2)), "%B %d, %Y").date()
            period_idx = i
            break
    if period_idx is None:
        raise ValueError("Regions: statement period line not found")

    # Product label: nearest all-caps line above the period line. Labels vary
    # across years — capture verbatim, never match on specific product names.
    label = ""
    for j in range(period_idx - 1, max(period_idx - 6, -1), -1):
        cand = lines[j].strip()
        if not cand or not re.search(r"[A-Z]", cand):
            continue
        if cand == cand.upper() and not re.match(r"^PAGE \d", cand, re.IGNORECASE):
            label = cand
            break
    return account_raw, label, period_start, period_end


def parse(pages: list[str], filename: str) -> list[ParsedStatement]:
    if not pages:
        raise ValueError("Regions: empty document")
    page1_lines = pages[0].splitlines()
    account_raw, label, period_start, period_end = _find_header(page1_lines)

    notes: list[str] = []
    if account_raw is None:
        raise ValueError("Regions: account number not found")
    account_full, last4 = split_account_number(account_raw)
    if not last4:
        raise ValueError(f"Regions: could not read account number from {account_raw!r}")

    opening = closing = None
    summary_figures: dict[str, Decimal] = {}
    for line in page1_lines:
        line = line.strip()
        if opening is None:
            v = _summary_amount(line, "Beginning Balance")
            if v is not None:
                opening = v
                continue
        if closing is None:
            v = _summary_amount(line, "Ending Balance")
            if v is not None:
                closing = v
                continue
        for summary_label in _SUMMARY_CROSSCHECKS:
            v = _summary_amount(line, summary_label)
            if v is not None:
                summary_figures[summary_label] = v
                break
    if opening is None or closing is None:
        raise ValueError("Regions: Beginning/Ending Balance not found in SUMMARY block")

    transactions: list[Transaction] = []
    section_sums: dict[str, Decimal] = {}
    section_totals: dict[str, Decimal] = {}
    current: tuple[str, str, int] | None = None  # (section name, tx_type, sign)
    last_tx: Transaction | None = None

    def enter(section_name: str | None) -> None:
        nonlocal current, last_tx
        if section_name is None:
            current = None
        else:
            tx_type, sign = SECTIONS[section_name]
            current = (section_name, tx_type, sign)
        last_tx = None

    done = False
    for page in pages:
        if done:
            break
        for raw_line in page.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _TERMINAL_RE.match(line):
                # Daily balances, balance-your-account worksheet, disclosures —
                # nothing after this is a transaction.
                done = True
                break

            header = re.sub(r"\s*\(CONTINUED\)$", "", line)
            if header in SECTIONS:
                if current is None or current[0] != header:
                    enter(header)
                continue

            m = _TOTAL_RE.match(line)
            if m and current is not None:
                total_label = re.sub(r"\s+", " ", m.group(1)).strip().upper()
                if total_label == current[0] or total_label in SECTIONS:
                    section_totals[total_label if total_label in SECTIONS else current[0]] = (
                        parse_money(m.group(2))
                    )
                    enter(None)
                    continue

            if current is None:
                continue
            section_name, tx_type, sign = current

            if any(rx.search(line) for rx in _NOISE_RES):
                continue

            if tx_type == TX_CHECK:
                if _CHECKS_COLHDR_RE.match(line):
                    continue
                matched = False
                for cm in _CHECK_TRIPLET_RE.finditer(line):
                    matched = True
                    mm, dd = (int(x) for x in cm.group(1).split("/"))
                    tx_date, warn = infer_year(mm, dd, period_start, period_end)
                    if warn:
                        notes.append(warn)
                    if tx_date is None:
                        continue
                    check_no = cm.group(2)
                    desc = f"Check {check_no}" + (" (out of sequence)" if cm.group(3) else "")
                    amount = -parse_money(cm.group(4))
                    transactions.append(
                        Transaction(tx_date, desc, amount, TX_CHECK, check_no, filename)
                    )
                    section_sums[section_name] = (
                        section_sums.get(section_name, Decimal("0")) + abs(amount)
                    )
                if not matched:
                    notes.append(f"Regions: unparsed line in CHECKS section: {line!r}")
                last_tx = None
                continue

            m = _TX_RE.match(line)
            if m:
                mm, dd = (int(x) for x in m.group(1).split("/"))
                tx_date, warn = infer_year(mm, dd, period_start, period_end)
                if warn:
                    notes.append(warn)
                if tx_date is None:
                    continue
                amount = sign * parse_money(m.group(3))
                last_tx = Transaction(
                    tx_date, m.group(2).strip(), amount, tx_type, None, filename
                )
                transactions.append(last_tx)
                section_sums[section_name] = (
                    section_sums.get(section_name, Decimal("0")) + abs(amount)
                )
                continue

            # Wrapped description continuation — join with a space.
            if last_tx is not None:
                last_tx.description += " " + line
            else:
                notes.append(
                    f"Regions: stray line in {section_name} section ignored: {line!r}"
                )

    # Cross-check per-section printed totals against what we parsed.
    for section_name, printed in section_totals.items():
        parsed_sum = section_sums.get(section_name, Decimal("0"))
        if parsed_sum != printed:
            notes.append(
                f"Regions: {section_name} parsed sum {money_str(parsed_sum)} "
                f"!= printed section total {money_str(printed)}"
            )

    # Cross-check the SUMMARY block figures.
    for summary_label, section_name in _SUMMARY_CROSSCHECKS.items():
        if summary_label in summary_figures:
            parsed_sum = section_sums.get(section_name, Decimal("0"))
            if parsed_sum != summary_figures[summary_label]:
                notes.append(
                    f"Regions: summary '{summary_label}' {money_str(summary_figures[summary_label])} "
                    f"!= parsed {section_name} sum {money_str(parsed_sum)}"
                )

    stmt = ParsedStatement(
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
    )

    # AUTOMATIC TRANSFERS direction isn't printed; default is debit. If the
    # statement only reconciles with transfers as credits, flip and say so.
    transfers = [t for t in transactions if t.tx_type == TX_TRANSFER]
    if transfers:
        delta = stmt.reconciliation_delta
        transfer_sum = sum((t.amount for t in transfers), Decimal("0"))
        if delta != 0 and transfer_sum != 0 and delta == -2 * transfer_sum:
            for t in transfers:
                t.amount = -t.amount
            notes.append(
                "Regions: AUTOMATIC TRANSFERS sign inferred as credit "
                "(statement reconciles only with transfers as credits)"
            )

    return [stmt.finalize()]
