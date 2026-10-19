"""Bank statement parser registry and per-file orchestration.

Adding a third bank = add a module here implementing
``matches(text) -> bool`` and ``parse(pages: list[str], filename) ->
list[ParsedStatement]``, then append it to ``PARSERS``. Nothing else
changes.
"""
from __future__ import annotations

import os
import re

from . import ocr as _ocr
from . import capitalcity, regions, servisfirst, wellsfargo
from .base import (
    STATUS_ENCRYPTED,
    STATUS_NO_TEXT,
    STATUS_OK,
    STATUS_PARSE_ERROR,
    STATUS_UNRECOGNIZED,
    FileResult,
    ParsedStatement,
    Transaction,
    money_str,
)

__all__ = [
    "PARSERS",
    "OCR_NOTE",
    "detect_bank",
    "extract_pages",
    "ocr_availability",
    "parse_pdf",
    "readable_text",
    "FileResult",
    "ParsedStatement",
    "Transaction",
]

# Order matters: the last-resort signatures are structural rather than a
# wordmark — ServisFirst's is a generic reverse-side disclosure footer, and
# Capital City prints no bank name outside a marketing blurb — so banks with
# an unambiguous wordmark are matched ahead of both, and Capital City (whose
# layout fallback is specific to its own section names) ahead of ServisFirst.
PARSERS = [regions, wellsfargo, capitalcity, servisfirst]


# A PDF whose embedded font carries no ToUnicode map extracts as a run of
# '(cid:12)' placeholders: pdfminer saying it could not tell what the glyph
# was. That is not text. A page of it has nothing to route, nothing to parse
# and nothing to report beyond the placeholders themselves, so it has to count
# as no text everywhere the pipeline asks the question — above all at the
# --ocr gate, which exists for exactly this file and was letting it through.
_CID_RE = re.compile(r"\(cid:\d+\)")


def readable_text(page: str) -> str:
    """``page`` with unmapped-glyph placeholders removed, stripped.

    Empty means the extractor produced nothing a parser could read, whether
    the page was blank or every character came back unmapped.
    """
    return _CID_RE.sub("", page or "").strip()


def _has_readable_text(pages: list[str] | None) -> bool:
    return bool(pages) and any(readable_text(p) for p in pages)


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


OCR_NOTE = (
    "OCR: this statement had no text layer — every figure above was "
    "machine-read from a scanned page. It reconciles, but spot-check it "
    "against the PDF."
)


def ocr_availability() -> tuple[bool, str]:
    """(usable, reason) for the --ocr path; see statement_parsers.ocr."""
    return _ocr.availability()


def _ocr_pages(path: str | os.PathLike, dpi: int) -> list[str] | None:
    """Rendered-and-machine-read page text, or None when OCR isn't possible."""
    return _ocr.ocr_pages(path, dpi=dpi)


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
    if not _has_readable_text(pages):
        detail = ("no extractable text (possible scan) — "
                  "retry with --ocr to machine-read it")
        if any(p.strip() for p in pages):
            # There *is* a text layer; every character in it came back
            # unmapped. Saying "no text" would send someone looking for a
            # scan that isn't there.
            detail = ("the text layer has no readable characters — its font "
                      "carries no character map, so every glyph extracts as "
                      "'(cid:N)'. Retry with --ocr to machine-read the page "
                      "images instead")
        return FileResult(filename, STATUS_NO_TEXT, detail=detail), None

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


def parse_pdf(path: str | os.PathLike, ocr: bool = False) -> FileResult:
    """Extract and parse one PDF. Never raises — one bad PDF must never
    abort the run; failures come back as a FileResult status.

    pdfplumber is the primary extractor. We retry with PyMuPDF only when the
    pdfplumber result is less than fully reconciled — i.e. it errored, wasn't
    recognized, or parsed but didn't reconcile (a symptom of a text layer
    pdfminer can only partly decode). A statement pdfplumber already parses
    AND reconciles is returned immediately and never re-extracted, so the
    common case can't regress. The PyMuPDF result is preferred only when it
    reconciles (or when pdfplumber produced nothing usable at all).

    ``ocr=True`` adds a last stage for PDFs with no usable text layer at all
    (scans): pages are rendered and machine-read. It is gated on the *text*,
    not on the outcome — it runs only when no extractor found any text, or
    when some page carried none while another did (a part-rescanned file).
    A file that has text but fails to parse or reconcile is a real problem to
    report, not a scan to re-read, and re-reading it as pixels would only cost
    minutes and blur the diagnosis. An OCR parse is imported only when it
    reconciles, and is then marked with ``via_ocr`` plus a spot-check note.
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
    if _has_readable_text(fitz_pages):
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
                result = fitz_result

    if ocr:
        sources = [src for src in (pages, fitz_pages) if src]
        has_text = any(_has_readable_text(src) for src in sources)
        if not has_text or any(_has_blank_page(src) for src in sources):
            ocr_result = _ocr_attempt(path, filename)
            # A failed OCR attempt replaces the original verdict only when the
            # original had nothing to say. A file whose text layer gave a real
            # diagnosis (UNRECOGNIZED, a parse error) keeps it: re-reading text
            # as pixels can only blur that, never sharpen it.
            if ocr_result is not None and (ocr_result.status == STATUS_OK or not has_text):
                return ocr_result

    return result


def extract_pages(
    path: str | os.PathLike, ocr: bool = False
) -> list[tuple[str, list[str]]]:
    """Every extractor's view of one PDF, for diagnosis (see --dump-text).

    Returns ``[(extractor name, pages), ...]`` in the order ``parse_pdf``
    would reach for them, so a dump shows what the parser was actually
    working from — including an extractor that returned nothing, which is
    itself the finding when a file won't route.

    OCR is run only when neither text extractor produced readable text, the
    same gate ``parse_pdf`` uses, and only at the first rung of the ladder:
    a dump is for reading the layout, not for making the figures add up.

    Never raises; an extractor that fails contributes an empty page list.
    """
    views: list[tuple[str, list[str]]] = []
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            views.append(("pdfplumber", [page.extract_text() or "" for page in pdf.pages]))
    except Exception as exc:  # noqa: BLE001 - a dump must never abort
        views.append((f"pdfplumber (failed: {type(exc).__name__})", []))

    fitz_pages = _pymupdf_pages(path)
    if fitz_pages is not None:
        views.append(("pymupdf", fitz_pages))

    if ocr and not any(_has_readable_text(pages) for _, pages in views):
        ocr_pages = _ocr_pages(path, _ocr.DEFAULT_DPI)
        if ocr_pages is not None:
            views.append((f"ocr {_ocr.DEFAULT_DPI}dpi", ocr_pages))

    return views


def _has_blank_page(pages: list[str]) -> bool:
    """A page with no text alongside a page that has some — i.e. part of the
    file was rescanned, so a text-layer parse is silently missing whole pages."""
    return (any(not readable_text(p) for p in pages)
            and any(readable_text(p) for p in pages))


# How informative a failed OCR attempt is, for picking which one to report.
# "Parsed, but $25.00 off" says where to look; "no bank signature recognized"
# only says the read was too poor to classify.
_DIAGNOSIS_RANK = {STATUS_OK: 2, STATUS_PARSE_ERROR: 1, STATUS_UNRECOGNIZED: 0}


def _diagnosis_key(result: FileResult):
    """Sort key for failed OCR attempts — larger is the better diagnosis.

    Rank first, then, among attempts that did parse, the one that came
    closest to balancing: a rung that is $0.04 out points at a single
    misread digit, where one that is $19,000 out could be anything.
    """
    shortfall = sum(abs(s.reconciliation_delta) for s in result.statements)
    return _DIAGNOSIS_RANK.get(result.status, 0), -shortfall


def _ocr_attempt(path: str | os.PathLike, filename: str) -> FileResult | None:
    """Render-and-machine-read the file. None means 'nothing better to offer'.

    Every rung of ``ocr.DPI_LADDER`` gets a turn until one reconciles, because
    no single render resolution reads every scan: over one real folder of 41
    scanned statements, 39 reconciled at 300 or 400 dpi, and the last two
    needed 200 and 500. Reconciliation picks the winner, so a wrong rung can't
    slip bad figures through — it simply doesn't add up and the next rung gets
    a turn. A statement that reads cleanly at the first rung stops there and
    costs no more than it did before.

    Only a *reconciling* OCR parse is imported: OCR misreads digits, and an
    unreconciled OCR result is a guess with wrong numbers in it, which is
    worse than a file honestly reported as unreadable.

    An OCR run that read the page but couldn't be trusted still reports *why*
    rather than falling back to a bare "no extractable text". The distinction
    matters to whoever has to fix the file: "the scan has no text" and "the
    scan read fine but its account number is blacked out" call for completely
    different actions, and only the second one is true here. When every
    resolution failed, the most informative of their verdicts is reported.
    """
    best: FileResult | None = None
    for dpi in _ocr.DPI_LADDER:
        pages = _ocr_pages(path, dpi)
        if not _has_readable_text(pages):
            continue
        ocr_result, _ = _run_pipeline(pages, filename)
        if _fully_reconciled(ocr_result):
            ocr_result.via_ocr = True
            for statement in ocr_result.statements:
                statement.notes.insert(0, OCR_NOTE)
            return ocr_result
        if best is None or _diagnosis_key(ocr_result) > _diagnosis_key(best):
            best = ocr_result

    if best is None:
        return None

    tried = f"at {len(_ocr.DPI_LADDER)} render resolutions"
    if best.status == STATUS_OK:
        # Parsed, but the figures don't add up — exactly what a misread digit
        # looks like. Report the discrepancy; never import the numbers.
        deltas = "; ".join(
            f"{money_str(s.reconciliation_delta)} off"
            for s in best.statements if not s.reconciled
        )
        detail = (
            f"OCR read this scan {tried} but the figures never reconcile "
            f"(closest attempt {deltas}) — likely a misread digit. Not "
            "imported; check the PDF or supply the original download."
        )
    elif best.status == STATUS_UNRECOGNIZED:
        detail = (
            f"OCR read this scan {tried} but no bank signature was recognized "
            f"in the result: {best.detail}"
        )
    else:
        detail = f"OCR read this scan {tried} but parsing failed — {best.detail}"
    return FileResult(filename, STATUS_PARSE_ERROR, detail=detail)
