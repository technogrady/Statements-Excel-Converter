"""Optional OCR fallback for scanned statements.

Statements that were printed and scanned back in carry no text layer at
all, so every text extractor returns nothing and the file is reported
``NO_TEXT (possible scan)``. With ``--ocr``, such files are rendered to
images and machine-read instead.

Two deliberate design choices:

* **No extra Python package.** Pages are rendered with PyMuPDF (already
  the optional second text extractor) and handed to the ``tesseract``
  binary directly. Nothing new to pip-install; the only requirement is
  Tesseract itself, which is looked up on PATH, in ``$TESSERACT_CMD``,
  and in the usual Windows/macOS install locations.
* **Page-segmentation mode 6** ("one uniform block of text"), with
  ``preserve_interword_spaces``. Tesseract's default mode detects
  columns and emits them one after another, which would tear a
  transaction table into a list of dates followed by a list of
  amounts. Mode 6 keeps each printed row on one line — the same shape
  the parsers already expect from ``pdfplumber``.
* **No single render resolution.** ``DPI_LADDER`` exists because there
  isn't one: the scanner's halftone beats against the sampling grid
  differently at each resolution, so the same page can read perfectly
  at 300 dpi, drop a whole line at 400, and come back at 200. Callers
  render at one rung, parse, and reach for the next only when the
  figures don't add up.

OCR is *machine reading*: a smudged ``8`` can come back as ``3``. The
parsers' own arithmetic is the guard — a misread amount breaks
reconciliation and the statement is flagged on the Inventory tab — and
every OCR'd statement additionally carries a spot-check note. Nothing
here ever raises: an unavailable or failing OCR path just declines.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Override the binary location (or name) without touching PATH.
TESSERACT_ENV = "TESSERACT_CMD"
# Page-segmentation mode; see the module docstring for why 6 is the default.
PSM_ENV = "TESSERACT_PSM"
DEFAULT_PSM = "6"
DEFAULT_DPI = 300
# Render resolutions to try, in order, until a statement reconciles. There is
# no single best one: the scanner's own halftone beats against the sampling
# grid differently at each, so a page that reads perfectly at 300 can lose a
# whole line at 400 and get it back at 200. Callers walk this ladder and stop
# at the first rung whose parse adds up (see statement_parsers._ocr_attempt),
# so a statement that reads cleanly at 300 never pays for the rest.
DPI_LADDER = (300, 400, 200, 500)
# Per-page ceiling. A 300-dpi letter page is a couple of seconds; anything
# near this means something is wrong, and one bad page must not hang a run.
PAGE_TIMEOUT = 180

_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
)

INSTALL_HINT = (
    "Install Tesseract OCR:\n"
    "  Windows:  winget install -e --id UB-Mannheim.TesseractOCR\n"
    "  macOS:    brew install tesseract\n"
    "  Linux:    sudo apt install tesseract-ocr\n"
    "then open a new terminal and re-run. If it is installed somewhere "
    f"unusual, point {TESSERACT_ENV} at the executable."
)


def find_tesseract() -> str | None:
    """Path to the tesseract executable, or None if it can't be found."""
    override = os.environ.get(TESSERACT_ENV)
    if override:
        return shutil.which(override) or (override if Path(override).is_file() else None)
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def availability() -> tuple[bool, str]:
    """(usable, reason) — reason explains what to install when unusable."""
    try:
        import fitz  # noqa: F401  (PyMuPDF, used to rasterize pages)
    except ImportError:
        return False, (
            "OCR needs PyMuPDF to render pages: pip install pymupdf"
        )
    if find_tesseract() is None:
        return False, f"OCR needs the Tesseract engine, which was not found. {INSTALL_HINT}"
    return True, ""


def _run_tesseract(exe: str, image: Path) -> str:
    """OCR one rendered page image to text. Raises on failure."""
    proc = subprocess.run(
        [
            exe, str(image), "stdout",
            "--psm", os.environ.get(PSM_ENV, DEFAULT_PSM),
            "-c", "preserve_interword_spaces=1",
        ],
        capture_output=True,
        timeout=PAGE_TIMEOUT,
        check=True,
    )
    return proc.stdout.decode("utf-8", errors="replace")


def ocr_pages(
    path: str | os.PathLike,
    dpi: int = DEFAULT_DPI,
    errors: list[str] | None = None,
) -> list[str] | None:
    """Render every page of ``path`` and OCR it; None if OCR isn't possible.

    Never raises — OCR is a best-effort recovery path, so a missing engine,
    an unreadable PDF or a tesseract crash all just decline.

    A page that fails to render or read costs only itself: it contributes
    empty text and the rest of the document still comes back. Losing a
    60-page statement to one bad page would be bad enough, but the real
    damage was that it looked identical to "this file has no text" — the
    caller got None either way and reported the text layer, never
    mentioning that OCR had been tried and died. Pass ``errors`` to collect
    what went wrong so it can be reported instead of guessed at.
    """
    def note(message: str) -> None:
        if errors is not None and message not in errors:
            errors.append(message)

    ok, reason = availability()
    if not ok:
        note(reason)
        return None
    exe = find_tesseract()
    if exe is None:  # pragma: no cover - availability() already checked
        return None
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - availability() checked
        note(f"PyMuPDF unavailable: {exc}")
        return None

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        note(f"could not open the PDF to render it: {type(exc).__name__}: {exc}")
        return None

    texts: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="stmt-ocr-") as tmp:
            tmpdir = Path(tmp)
            for index in range(doc.page_count):
                try:
                    # Grayscale: statements are black text on white, and it
                    # cuts the render a third without hurting recognition.
                    pixmap = doc[index].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                    image = tmpdir / f"page{index + 1}.png"
                    pixmap.save(image)
                    texts.append(_run_tesseract(exe, image))
                except Exception as exc:  # noqa: BLE001 - one page, not the file
                    texts.append("")
                    note(f"page {index + 1} at {dpi} dpi: "
                         f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        doc.close()
    return texts
