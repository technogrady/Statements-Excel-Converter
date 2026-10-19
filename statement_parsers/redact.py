"""Strip identifying detail out of extracted statement text.

A statement that won't parse can only be diagnosed from the text the
extractor actually produced — but that text is somebody's bank
statement. This module removes the parts that identify the holder so the
rest can be shared with whoever is fixing the parser:

* **account and routing numbers**, anywhere on the page. Every run of 7
  or more digits becomes ``#``\\ s of the same length, and the digits
  inside a bank-printed mask (``XXXXXXX5255``) go too. Length is
  preserved because a parser cares about the shape, never the value.
* **the holder's name and mailing address**, which live in the block of
  page furniture every layout reprints at the top. Address lines are
  found by shape — a city/ST/ZIP, a street line, a PO box — the name
  line above them goes with them, and so does the free text to the left
  of a header field (``53 REDWOOD LN   Primary Account XXXXXXX5255``
  keeps only its right half).

Everything else is left alone: section headers, column rules, dates,
amounts, merchant names and descriptions. Note what that means — a
redacted dump still shows what was spent and where. It is meant for
someone fixing the parser, not for publishing.

Three deliberate limits, each a trade:

* **Only the header block is swept for addresses.** Below it, a wrapped
  transaction description is routinely a merchant name followed by its
  street (``LOWE S #417`` / ``2121 NE CAPITOL CIRCLE``), and those lines
  are the whole reason the wrap logic exists — redacting them would hide
  the bug being diagnosed. They are a shop's address, not the holder's.
  A city/ST/ZIP is redacted wherever it appears regardless, since a real
  ZIP identifies a real place.
* **A holder name printed in a right-hand column survives.** Some
  layouts interleave it with figures (Wells Fargo prints it beside the
  opening balance), where no shape tells it apart from the statement.
* **Redaction is by shape, so it is not a guarantee.** Read a dump
  before sending it on.
"""
from __future__ import annotations

import re

NAME_PLACEHOLDER = "<NAME REDACTED>"
ADDRESS_PLACEHOLDER = "<ADDRESS REDACTED>"

# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

# Account and routing numbers are long unbroken digit runs. Seven is the
# floor: a check number is four, a ZIP five, 'MM/DD/YY' splits into twos and
# an amount splits at its separators, so nothing a parser reads as a figure
# reaches this length. Transaction reference numbers do, and are masked too —
# their shape is all a parser needs from them.
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{7,}(?!\d)")
# The bank's own mask still ends in four real digits of the account.
_MASKED_ACCOUNT_RE = re.compile(r"(?<!\w)([Xx*•●]{3,})(\d{2,})")


def _mask_numbers(line: str) -> str:
    line = _MASKED_ACCOUNT_RE.sub(lambda m: m.group(1) + "#" * len(m.group(2)), line)
    return _LONG_DIGITS_RE.sub(lambda m: "#" * len(m.group(0)), line)


# ---------------------------------------------------------------------------
# Addresses and names
# ---------------------------------------------------------------------------

_STREET_WORD = (
    r"ST|STREET|AVE|AVENUE|RD|ROAD|LN|LANE|DR|DRIVE|BLVD|BOULEVARD|CT|COURT"
    r"|WAY|HWY|HIGHWAY|PKWY|PARKWAY|TRL|TRAIL|CIR|CIRCLE|PL|PLACE|TER|TERRACE"
    r"|SQ|SQUARE|LOOP|RUN|PATH|XING|CROSSING|PIKE|ALY|ALLEY"
)
# 'ANYTOWN FL 00000-0000' / 'Anytown, ST 00000'. Redacted everywhere, header
# or body: a real ZIP points at a real place.
_CITY_STATE_ZIP_RE = re.compile(
    r"[A-Za-z][A-Za-z.' -]{1,30},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?"
)
# '53 REDWOOD LN', '12 EXAMPLE LANE' — header block only.
_STREET_RE = re.compile(
    rf"\b\d{{1,6}}\s+(?:[NSEW]{{1,2}}\s+)?[A-Za-z][A-Za-z0-9.' -]{{0,40}}?"
    rf"\b(?:{_STREET_WORD})\b\.?",
    re.IGNORECASE,
)
_POBOX_RE = re.compile(r"\bP\.?\s*O\.?\s*BOX\s+[\w-]+", re.IGNORECASE)
_UNIT_RE = re.compile(r"\b(?:APT|STE|SUITE|UNIT)\.?\s+[\w-]+", re.IGNORECASE)

_HEADER_ADDRESS_RES = (_CITY_STATE_ZIP_RE, _STREET_RE, _POBOX_RE, _UNIT_RE)

# A field the parser reads, printed to the right of the holder block on the
# same line. Everything to its left is the holder's.
_HEADER_FIELD_RE = re.compile(
    r"\b(?:Primary\s+Acc(?:oun)?t\.?|Account\s+Number|Account\s*#|Statement\s+Dates?"
    r"|Date\s+\d{1,2}/\d{1,2}|Page\s+\d)",
    re.IGNORECASE,
)

# Words that mark text as the statement's own furniture rather than a name,
# so neither the walk upward from an address nor the left-column rule eats a
# field the parser needs.
_STATEMENT_VOCAB_RE = re.compile(
    r"\b(?:account|balance|statement|date|dates|page|primary|acct|deposit|deposits"
    r"|credit|credits|debit|debits|check|checks|withdrawal|withdrawals|interest"
    r"|service|charge|charges|amount|description|total|totals|continued|images"
    r"|period|days|avg|average|ledger|collected|previous|ending|beginning|closing"
    r"|summary|transaction|history|fdic|member|notice|bank)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?://|\b\w+\.(?:com|net|org|gov|co\.uk)\b)", re.IGNORECASE)
_MONEY_RE = re.compile(r"\d[\d,]*[.,:]\d{2}(?!\d)")
_SHORT_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_LONG_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4}",
    re.IGNORECASE,
)
# The first transaction row ends the header block.
_TABLE_ROW_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}[\s/]")
# ...and if no row turns up, the block cannot run on forever.
_MAX_HEADER_LINES = 20


def _looks_like_a_name(text: str) -> bool:
    """Is this free text plausibly the holder's, rather than the statement's?

    Deliberately narrow. Anything carrying a money amount, a date in either
    shape, or the statement's own vocabulary is page furniture or a
    transaction and must survive, or the dump stops being readable.
    """
    text = text.strip(" \t|,")
    if not text or not re.search(r"[A-Za-z]", text) or len(text) > 60:
        return False
    # A name is a handful of words, not prose. Marketing copy and URLs sit in
    # the same header block, and the bank's own name in them is what routes
    # the file — losing it would hide the very thing a dump is read for.
    if len(text.split()) > 8 or _URL_RE.search(text):
        return False
    if _MONEY_RE.search(text) or _SHORT_DATE_RE.search(text) or _LONG_DATE_RE.search(text):
        return False
    return not _STATEMENT_VOCAB_RE.search(text)


def _header_end(lines: list[str]) -> int:
    """Index of the first body line — the holder block is above it."""
    for i, line in enumerate(lines[:_MAX_HEADER_LINES]):
        if _TABLE_ROW_RE.match(line):
            return i
    return min(len(lines), _MAX_HEADER_LINES)


def _redact_header_line(line: str) -> tuple[str, bool]:
    """Strip address fragments and any holder text left of a header field.

    Reports whether an *address* matched, which is what tells the caller a
    mailing block starts here and the line above it may be the name.
    """
    found = False
    for pattern in _HEADER_ADDRESS_RES:
        line, count = pattern.subn(ADDRESS_PLACEHOLDER, line)
        found = found or bool(count)

    field = _HEADER_FIELD_RE.search(line)
    if field and field.start() > 0:
        prefix = line[:field.start()]
        if ADDRESS_PLACEHOLDER in prefix:
            line = ADDRESS_PLACEHOLDER + " " + line[field.start():]
        elif _looks_like_a_name(prefix):
            line = NAME_PLACEHOLDER + " " + line[field.start():]
    return line, found


def redact_text(text: str) -> str:
    """Redact one page of extracted statement text."""
    lines = (text or "").splitlines()
    cut = _header_end(lines)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i < cut:
            line, hit_address = _redact_header_line(line)
            if hit_address:
                # The name sits directly above the address block — walk back
                # over what we have already emitted and take it with us. Only
                # an *address* starts this walk: when the holder's name shared
                # a line with a header field it has already gone, and walking
                # on from there would eat the lines above it too.
                for j in range(len(out) - 1, max(len(out) - 3, -1), -1):
                    if ADDRESS_PLACEHOLDER in out[j] or NAME_PLACEHOLDER in out[j]:
                        continue
                    if not _looks_like_a_name(out[j]):
                        break
                    out[j] = NAME_PLACEHOLDER
        else:
            line = _CITY_STATE_ZIP_RE.sub(ADDRESS_PLACEHOLDER, line)
        out.append(_mask_numbers(line))
    # splitlines() drops the final terminator; put it back so a redacted page
    # is the same shape as the one that went in.
    return "\n".join(out) + ("\n" if text and text.endswith(("\n", "\r")) else "")


def redact_pages(pages: list[str]) -> list[str]:
    return [redact_text(page) for page in pages]
