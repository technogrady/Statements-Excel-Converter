#!/usr/bin/env python3
"""Bank Statement → Excel consolidation tool.

Parse a folder of bank statement PDFs (digital text, not scanned) and
produce a single Excel workbook: an Inventory tab (one row per
statement, with per-account coverage summary) plus one worksheet per
bank account with every transaction, date-sorted and deduplicated at
the statement level.

Usage:
    python parse_statements.py <input_folder> [-o output.xlsx]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from consolidation import consolidate
from excel_writer import write_workbook
from statement_parsers import parse_pdf
from statement_parsers.base import STATUS_OK


def find_pdfs(folder: Path) -> list[Path]:
    """All PDFs under the folder (recursive), deterministic order."""
    return sorted(
        (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: str(p).lower(),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Consolidate bank statement PDFs into one Excel workbook."
    )
    ap.add_argument("input_folder", help="folder containing statement PDFs (searched recursively)")
    ap.add_argument(
        "-o", "--output", default="Bank_Statements.xlsx",
        help="output workbook path (default: Bank_Statements.xlsx)",
    )
    args = ap.parse_args(argv)

    folder = Path(args.input_folder)
    if not folder.is_dir():
        print(f"error: {folder} is not a directory", file=sys.stderr)
        return 2
    pdfs = find_pdfs(folder)
    if not pdfs:
        print(f"error: no PDF files found under {folder}", file=sys.stderr)
        return 2

    print(f"Scanning {len(pdfs)} PDF file(s) in {folder} ...")
    results = []
    for path in pdfs:
        result = parse_pdf(path)
        results.append(result)
        if result.status != STATUS_OK:
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
    print(f"  workbook written:   {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
