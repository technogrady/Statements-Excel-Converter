#!/usr/bin/env python3
"""Bank Statement → Excel consolidation tool.

Parse a folder of bank statement PDFs and produce a single Excel
workbook: an Inventory tab (one row per statement, with per-account
coverage summary) plus one worksheet per bank account with every
transaction, date-sorted and deduplicated at the statement level.

Statements are expected to carry a text layer; --ocr additionally
machine-reads scanned ones (see statement_parsers/ocr.py).

Usage:
    python parse_statements.py <input> [-o output.xlsx] [--ocr]
    python parse_statements.py <input> --dump-text [--pages N] [--ocr]

``<input>`` is a folder (searched recursively) or a single PDF.
``--dump-text`` writes no workbook: it prints the text each extractor
produced, with the holder's name, address and account numbers removed,
so a statement that won't parse can be diagnosed without handing anyone
the statement (see statement_parsers/redact.py).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from consolidation import consolidate
from excel_writer import write_workbook
from statement_parsers import extract_pages, ocr_availability, parse_pdf
from statement_parsers.base import STATUS_NO_TEXT, STATUS_OK
from statement_parsers.redact import redact_text


def find_pdfs(target: Path) -> list[Path]:
    """The PDFs to process: one file, or everything under a folder
    (recursive), in deterministic order."""
    if target.is_file():
        return [target] if target.suffix.lower() == ".pdf" else []
    return sorted(
        (p for p in target.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: str(p).lower(),
    )


def dump_text(pdfs: list[Path], use_ocr: bool, max_pages: int | None) -> int:
    """Print a redacted dump of what each extractor read. Writes no workbook.

    This is the diagnostic path: when a statement won't parse, the only thing
    that explains why is the text the extractor actually produced, and that
    text is somebody's bank statement. Everything printed here has been
    through redact_text first — but read it before sending it on. It still
    shows amounts, merchants and dates; redaction removes identity, not the
    statement.
    """
    for path in pdfs:
        print(f"########## {path.name} ##########")
        for name, pages in extract_pages(path, ocr=use_ocr):
            shown = pages if max_pages is None else pages[:max_pages]
            total = (f"{len(pages)} page(s)" if max_pages is None or len(pages) <= max_pages
                     else f"first {len(shown)} of {len(pages)} page(s)")
            readable = sum(1 for page in pages if page.strip())
            print(f"===== extractor: {name} — {total}, "
                  f"{readable} with any text =====")
            for index, page in enumerate(shown, 1):
                print(f"----- page {index} -----")
                body = redact_text(page).strip()
                print(body if body else "(no text)")
        print()
    print("Redaction removes the holder's name, mailing address and account "
          "numbers. Amounts, dates and merchant names are kept — read the "
          "dump before sharing it.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Consolidate bank statement PDFs into one Excel workbook."
    )
    ap.add_argument(
        "input_folder",
        help="folder containing statement PDFs (searched recursively), "
             "or a single PDF",
    )
    ap.add_argument(
        "-o", "--output", default="Bank_Statements.xlsx",
        help="output workbook path (default: Bank_Statements.xlsx)",
    )
    ap.add_argument(
        "--ocr", action="store_true",
        help="machine-read scanned statements that have no text layer "
             "(needs Tesseract OCR installed). Slower, and OCR'd figures are "
             "flagged for spot-checking; a statement that doesn't reconcile "
             "after OCR is reported unread rather than imported.",
    )
    ap.add_argument(
        "--dump-text", action="store_true",
        help="write no workbook: print the text each extractor read, with "
             "the holder's name, address and account numbers removed. Use it "
             "to diagnose a statement that won't parse without handing over "
             "the statement. Amounts and merchant names are kept.",
    )
    ap.add_argument(
        "--pages", type=int, default=None, metavar="N",
        help="with --dump-text, print only the first N pages of each file "
             "(the layout is usually clear from the first two).",
    )
    args = ap.parse_args(argv)

    folder = Path(args.input_folder)
    if not folder.exists():
        print(f"error: {folder} does not exist", file=sys.stderr)
        return 2
    pdfs = find_pdfs(folder)
    if not pdfs:
        print(f"error: no PDF files found at {folder}", file=sys.stderr)
        return 2
    if args.pages is not None and args.pages < 1:
        print("error: --pages must be 1 or more", file=sys.stderr)
        return 2

    use_ocr = args.ocr
    if use_ocr:
        ok, reason = ocr_availability()
        if not ok:
            print(f"warning: --ocr requested but unavailable, continuing without it.\n{reason}",
                  file=sys.stderr)
            use_ocr = False
        else:
            # --dump-text's stdout is meant to be pasted somewhere, so its
            # progress chatter goes to stderr.
            print("OCR enabled for statements with no text layer (this is slow).",
                  file=sys.stderr if args.dump_text else sys.stdout)

    if args.dump_text:
        return dump_text(pdfs, use_ocr, args.pages)

    print(f"Scanning {len(pdfs)} PDF file(s) in {folder} ...")
    results = []
    for path in pdfs:
        result = parse_pdf(path, ocr=use_ocr)
        results.append(result)
        if result.via_ocr:
            print(f"  [OCR] {result.source_file}: machine-read — spot-check the figures")
        elif result.status != STATUS_OK:
            print(f"  [{result.status}] {result.source_file}: {result.detail[:90]}")

    consolidation = consolidate(results)
    sheet_names = write_workbook(consolidation, args.output)

    # ---- run summary -------------------------------------------------------
    status_counts = Counter(r.status for r in results)
    statements = [s for r in results if r.status == STATUS_OK for s in r.statements]
    n_dupes = len(consolidation.duplicates)
    n_failures = len(results) - status_counts[STATUS_OK]
    unreconciled = [
        s for g in consolidation.groups for s in g.statements if not s.reconciled
    ]

    print()
    print("Run summary")
    print(f"  files processed:    {len(results)}")
    print(f"  statements parsed:  {len(statements)}")
    print(f"  duplicates:         {n_dupes}")
    n_ocr = sum(1 for r in results if r.via_ocr)
    if n_ocr:
        print(f"  read by OCR:        {n_ocr}  (flagged on the Inventory tab)")
    print(f"  failures:           {n_failures}"
          + (f"  ({', '.join(f'{k}={v}' for k, v in sorted(status_counts.items()) if k != STATUS_OK)})"
             if n_failures else ""))
    print(f"  accounts found:     {len(consolidation.groups)}")
    for g in consolidation.groups:
        n_tx = sum(len(s.transactions) for s in g.statements)
        print(f"    {sheet_names[g.sheet_base_name]}: {len(g.statements)} statement(s), "
              f"{n_tx} transaction(s), chain: {g.chain_status}"
              + (f", missing months: {', '.join(g.missing_months)}" if g.missing_months else ""))
    if unreconciled:
        print(f"  ⚠ reconciliation FAILED for {len(unreconciled)} statement(s) — see Inventory tab")
    if not use_ocr and any(r.status == STATUS_NO_TEXT for r in results):
        print("  note: some files look like scans — re-run with --ocr to machine-read them")
    print(f"  workbook written:   {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
