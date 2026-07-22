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


def _pymupdf_pages(path: str | os.PathLike) -> list[str] | None:
    """Fallback extractor for PDFs whose text layer pdfminer can't decode.

    Some statements (e.g. ServisFirst 'Enhanced' exports) carry a real,
    copyable text layer that pdfplumber/pdfminer returns almost nothing for,
    while PyMuPDF reads it cleanly. PyMuPDF is an optional dependency; return
    None when it isn't installed or can't open the file, so extraction never
    depends on it and never raises here.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        with fitz.open(path) as doc:
            return [doc[i].get_text() for i in range(doc.page_count)]
    except Exception:  # noqa: BLE001 - the fallback must never abort a run
        return None


def _parse_with(parser, pages: list[str], filename: str) -> FileResult:
    """Run an already-chosen parser over an extracted page list."""
    try:
        statements = parser.parse(pages, filename)
        if not statements:
            raise ValueError(f"{parser.BANK}: parser returned no statements")
        return FileResult(filename, STATUS_OK, statements=statements)
    except Exception as exc:  # noqa: BLE001
        return FileResult(filename, STATUS_PARSE_ERROR,
                          detail=f"{type(exc).__name__}: {exc}")


def _run_pipeline(pages: list[str], filename: str) -> tuple[FileResult, object | None]:
    """Classify + route + parse one page list. Returns (result, parser) so the
    caller can reuse a successfully-detected parser for a fallback re-extract."""
    if not any(p.strip() for p in pages):
        return (FileResult(filename, STATUS_NO_TEXT,
                           detail="no extractable text (possible scan)"), None)

    parser = detect_bank(pages[0]) or detect_bank("\n".join(pages))
    if parser is None:
        hint = " ".join("\n".join(pages).split())[:100]
        return FileResult(filename, STATUS_UNRECOGNIZED, detail=hint), None

    return _parse_with(parser, pages, filename), parser


def _fully_reconciled(result: FileResult) -> bool:
    return (
        result.status == STATUS_OK
        and bool(result.statements)
        and all(s.reconciled for s in result.statements)
    )


def parse_pdf(path: str | os.PathLike) -> FileResult:
    """Extract and parse one PDF. Never raises — one bad PDF must never
    abort the run; failures come back as a FileResult status.

    pdfplumber is the primary extractor. We retry with PyMuPDF only when the
    pdfplumber result is less than fully reconciled — i.e. it errored, wasn't
    recognized, or parsed but didn't reconcile (a symptom of a text layer
    pdfminer can only partly decode). A statement pdfplumber already parses
    AND reconciles is returned immediately and never re-extracted, so the
    common case can't regress. The PyMuPDF result is preferred only when it
    reconciles (or when pdfplumber produced nothing usable at all).
    """
    filename = os.path.basename(str(path))
    pages: list[str] | None = None
    detected = None
    result = FileResult(filename, STATUS_PARSE_ERROR,
                        detail="pdfplumber produced no pages")
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - per-file isolation is the contract
        if _looks_encrypted(exc):
            return FileResult(filename, STATUS_ENCRYPTED,
                              detail=f"{type(exc).__name__}: {exc}")
        # pdfplumber couldn't open it; the PyMuPDF fallback below gets a turn.
        result = FileResult(filename, STATUS_PARSE_ERROR,
                            detail=f"{type(exc).__name__}: {exc}")

    if pages is not None:
        result, detected = _run_pipeline(pages, filename)
        if _fully_reconciled(result):
            return result

    # Fallback: some PDFs carry a copyable text layer pdfminer can only partly
    # decode — the statement may parse yet miss transactions and fail to
    # reconcile. Retry with PyMuPDF; pdfplumber may already have routed the
    # file (e.g. via a footer signature), so reuse that parser when available.
    fitz_pages = _pymupdf_pages(path)
    if fitz_pages is not None and any(p.strip() for p in fitz_pages):
        parser = detected or detect_bank(fitz_pages[0]) or detect_bank(
            "\n".join(fitz_pages)
        )
        if parser is not None:
            fitz_result = _parse_with(parser, fitz_pages, filename)
            # Prefer PyMuPDF when it reconciles, or when pdfplumber produced no
            # usable parse at all (a parsed-but-unreconciled result still beats
            # an error). Never override a reconciling pdfplumber parse (we'd
            # have returned already above).
            if _fully_reconciled(fitz_result):
                return fitz_result
            if result.status != STATUS_OK and fitz_result.status == STATUS_OK:
                return fitz_result

    return result
