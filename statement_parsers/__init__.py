"""Bank statement parser registry and per-file orchestration.

Adding a third bank = add a module here implementing
``matches(text) -> bool`` and ``parse(pages: list[str], filename) ->
list[ParsedStatement]``, then append it to ``PARSERS``. Nothing else
changes.
"""
from __future__ import annotations

import os

from . import regions, servisfirst
from .base import (
    STATUS_ENCRYPTED,
    STATUS_NO_TEXT,
    STATUS_OK,
    STATUS_PARSE_ERROR,
    STATUS_UNRECOGNIZED,
    FileResult,
    ParsedStatement,
    Transaction,
)

__all__ = [
    "PARSERS",
    "detect_bank",
    "parse_pdf",
    "FileResult",
    "ParsedStatement",
    "Transaction",
]

PARSERS = [regions, servisfirst]


def detect_bank(text: str):
    """Route page-1 text to a parser module, or None if no signature matches."""
    for parser in PARSERS:
        if parser.matches(text):
            return parser
    return None


def _looks_encrypted(exc: BaseException) -> bool:
    # pdfplumber wraps pdfminer's PDFPasswordIncorrect (often with an empty
    # message) — walk the exception chain looking for password/encryption hints.
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__.lower()
        text = str(cur).lower()
        if any(word in name or word in text for word in ("password", "encrypt", "decrypt")):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def parse_pdf(path: str | os.PathLike) -> FileResult:
    """Extract and parse one PDF. Never raises — one bad PDF must never
    abort the run; failures come back as a FileResult status."""
    filename = os.path.basename(str(path))
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - per-file isolation is the contract
        if _looks_encrypted(exc):
            return FileResult(filename, STATUS_ENCRYPTED,
                              detail=f"{type(exc).__name__}: {exc}")
        return FileResult(filename, STATUS_PARSE_ERROR,
                          detail=f"{type(exc).__name__}: {exc}")

    if not any(p.strip() for p in pages):
        return FileResult(filename, STATUS_NO_TEXT,
                          detail="no extractable text (possible scan)")

    parser = detect_bank(pages[0]) or detect_bank("\n".join(pages))
    if parser is None:
        hint = " ".join("\n".join(pages).split())[:100]
        return FileResult(filename, STATUS_UNRECOGNIZED, detail=hint)

    try:
        statements = parser.parse(pages, filename)
        if not statements:
            raise ValueError(f"{parser.BANK}: parser returned no statements")
        return FileResult(filename, STATUS_OK, statements=statements)
    except Exception as exc:  # noqa: BLE001
        return FileResult(filename, STATUS_PARSE_ERROR,
                          detail=f"{type(exc).__name__}: {exc}")
