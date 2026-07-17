"""Workbook generation: an Inventory tab (first) plus one transaction
tab per bank account.

All money arrives as Decimal; floats appear only at the cell-write
boundary (xlsxwriter needs native numbers — the display format keeps
two decimals).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pandas as pd
import xlsxwriter

from consolidation import (
    CHAIN_CONTINUOUS,
    CHAIN_DISCONTINUITY,
    CHAIN_SINGLE,
    AccountGroup,
    Consolidation,
    total_credits,
    total_debits,
)
from statement_parsers.base import (
    STATUS_ENCRYPTED,
    STATUS_NO_TEXT,
    STATUS_OK,
    STATUS_PARSE_ERROR,
    STATUS_UNRECOGNIZED,
    ParsedStatement,
    money_str,
)

_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")

INVENTORY_COLUMNS = [
    "File", "Bank", "Account", "Account Type", "Period Start", "Period End",
    "Opening Balance", "Closing Balance", "# Transactions",
    "Total Credits", "Total Debits", "Reconciled", "Notes",
]
_MONEY_COLS = {"Opening Balance", "Closing Balance", "Total Credits", "Total Debits"}
_DATE_COLS = {"Period Start", "Period End"}

TX_COLUMNS = [
    "Date", "Type", "Check No", "Description", "Amount", "Statement Period", "Source File",
]

_FAILURE_LABELS = {
    STATUS_UNRECOGNIZED: "UNRECOGNIZED",
    STATUS_NO_TEXT: "NO_TEXT (possible scan)",
    STATUS_ENCRYPTED: "ENCRYPTED",
    STATUS_PARSE_ERROR: "PARSE ERROR",
}


def sheet_name_for(base: str, taken: set[str]) -> str:
    """Excel sheet name: strip forbidden chars ``[]:*?/\\``, cap at 31 chars,
    keep unique."""
    name = _SHEET_FORBIDDEN.sub("-", base).strip("'") or "Sheet"
    name = name[:31]
    if name.lower() in taken:
        for i in range(2, 1000):
            suffix = f"~{i}"
            cand = name[: 31 - len(suffix)] + suffix
            if cand.lower() not in taken:
                name = cand
                break
    taken.add(name.lower())
    return name


def write_workbook(consolidation: Consolidation, output_path: str) -> dict[str, str]:
    """Write the workbook; returns {sheet_base_name: actual sheet name}."""
    wb = xlsxwriter.Workbook(output_path, {"default_date_format": "mm/dd/yyyy"})
    fmt = {
        "header": wb.add_format({"bold": True, "bottom": 1, "bg_color": "#DDEBF7"}),
        "title": wb.add_format({"bold": True, "font_size": 12}),
        "money": wb.add_format({"num_format": "#,##0.00"}),
        "amount": wb.add_format({"num_format": "#,##0.00;[Red](#,##0.00)"}),
        "date": wb.add_format({"num_format": "mm/dd/yyyy"}),
        "ok": wb.add_format({"bg_color": "#C6EFCE", "font_color": "#006100"}),
        "bad": wb.add_format({"bg_color": "#FFC7CE", "font_color": "#9C0006"}),
        "warn": wb.add_format({"bg_color": "#FFEB9C", "font_color": "#9C6500"}),
        "wrap": wb.add_format({"text_wrap": False}),
    }

    taken: set[str] = {"inventory"}
    inventory_ws = wb.add_worksheet("Inventory")  # first tab, always

    sheet_names: dict[str, str] = {}
    for g in consolidation.groups:
        sheet_names[g.sheet_base_name] = sheet_name_for(g.sheet_base_name, taken)

    _write_inventory(inventory_ws, consolidation, sheet_names, fmt)
    for g in consolidation.groups:
        _write_account_sheet(wb, sheet_names[g.sheet_base_name], g, fmt)

    wb.close()
    return sheet_names


# ---------------------------------------------------------------------------
# Inventory tab
# ---------------------------------------------------------------------------

def _status_format(status_text: str, fmt: dict):
    if status_text == "OK":
        return fmt["ok"]
    if status_text.startswith("FAILED") or status_text.startswith("PARSE ERROR"):
        return fmt["bad"]
    return fmt["warn"]


def _inventory_rows(consolidation: Consolidation, group_of: dict[int, AccountGroup]):
    """One row per parsed statement (incl. duplicates) + one per failed file,
    sorted bank → account → period_start; failures last."""
    rows = []
    dup_kept_by = {id(s): kept for s, kept in consolidation.duplicates}

    all_statements: list[ParsedStatement] = []
    for fr in consolidation.file_results:
        if fr.status == STATUS_OK:
            all_statements.extend(fr.statements)

    for stmt in all_statements:
        dup_of = dup_kept_by.get(id(stmt))
        if dup_of is not None:
            status = "DUPLICATE"
            notes = f"DUPLICATE of {dup_of} — transactions excluded from account sheets"
            if stmt.notes:
                notes += "; " + "; ".join(stmt.notes)
            account_disp = f"x{stmt.account_last4}"
        elif stmt.reconciled:
            status = "OK"
            notes = "; ".join(stmt.notes)
            account_disp = group_of[id(stmt)].display_account
        else:
            status = f"FAILED (Δ {money_str(stmt.reconciliation_delta)})"
            notes = "; ".join(stmt.notes)
            account_disp = group_of[id(stmt)].display_account
        rows.append({
            "File": stmt.source_file,
            "Bank": stmt.bank,
            "Account": account_disp,
            "Account Type": stmt.account_label,
            "Period Start": stmt.period_start,
            "Period End": stmt.period_end,
            "Opening Balance": stmt.opening_balance,
            "Closing Balance": stmt.closing_balance,
            "# Transactions": len(stmt.transactions),
            "Total Credits": total_credits(stmt),
            "Total Debits": total_debits(stmt),
            "Reconciled": status,
            "Notes": notes,
            "_sort": (0, stmt.bank, stmt.account_last4, stmt.period_start),
        })

    for fr in consolidation.file_results:
        if fr.status == STATUS_OK:
            continue
        label = _FAILURE_LABELS.get(fr.status, fr.status)
        notes = fr.detail
        if fr.status == STATUS_UNRECOGNIZED:
            notes = f"first ~100 chars of extracted text: {fr.detail!r}"
        elif fr.status == STATUS_PARSE_ERROR:
            notes = f"PARSE ERROR: {fr.detail}"
        rows.append({
            "File": fr.source_file, "Bank": "", "Account": "", "Account Type": "",
            "Period Start": None, "Period End": None, "Opening Balance": None,
            "Closing Balance": None, "# Transactions": None, "Total Credits": None,
            "Total Debits": None, "Reconciled": label, "Notes": notes,
            "_sort": (1, "", "", fr.source_file),
        })

    rows.sort(key=lambda r: r["_sort"])
    return rows


def _write_inventory(ws, consolidation: Consolidation, sheet_names: dict[str, str], fmt: dict):
    group_of: dict[int, AccountGroup] = {}
    for g in consolidation.groups:
        for s in g.statements:
            group_of[id(s)] = g

    rows = _inventory_rows(consolidation, group_of)
    df = pd.DataFrame(rows, columns=INVENTORY_COLUMNS + ["_sort"]).drop(columns="_sort")

    widths = {c: len(c) for c in INVENTORY_COLUMNS}
    for ci, col in enumerate(INVENTORY_COLUMNS):
        ws.write(0, ci, col, fmt["header"])
    for ri, row in enumerate(df.itertuples(index=False), start=1):
        for ci, col in enumerate(INVENTORY_COLUMNS):
            _write_cell(ws, ri, ci, getattr(row, row._fields[ci]), col, fmt)
            widths[col] = max(widths[col], _cell_width(getattr(row, row._fields[ci]), col))

    n_rows = len(df)
    ws.freeze_panes(1, 0)
    if n_rows:
        ws.autofilter(0, 0, n_rows, len(INVENTORY_COLUMNS) - 1)

    # ---- Coverage Summary block: the per-account synopsis -----------------
    start = n_rows + 3
    ws.write(start, 0, "COVERAGE SUMMARY — one row per account", fmt["title"])
    cov_cols = [
        "Account", "Bank", "Account Number", "Labels (period each used)",
        "First Period Start", "Last Period End", "Statements",
        "Balance Chain", "Missing Months", "Notes",
    ]
    for ci, col in enumerate(cov_cols):
        ws.write(start + 1, ci, col, fmt["header"])
    for ri, g in enumerate(consolidation.groups, start=start + 2):
        labels = "; ".join(run.display() for run in g.label_runs)
        chain_fmt = (
            fmt["ok"] if g.chain_status in (CHAIN_CONTINUOUS, CHAIN_SINGLE)
            else fmt["bad"] if g.chain_status == CHAIN_DISCONTINUITY
            else fmt["warn"]
        )
        values = [
            sheet_names[g.sheet_base_name],
            g.bank,
            g.account_number_display,
            labels,
            g.statements[0].period_start,
            max(s.period_end for s in g.statements),
            len(g.statements),
            g.chain_status,
            ", ".join(g.missing_months) if g.missing_months else "(none)",
            "; ".join(g.chain_notes),
        ]
        for ci, (col, val) in enumerate(zip(cov_cols, values)):
            if isinstance(val, date):
                ws.write_datetime(ri, ci, val, fmt["date"])
            elif col == "Balance Chain":
                ws.write(ri, ci, val, chain_fmt)
            else:
                ws.write(ri, ci, val)
            widths[INVENTORY_COLUMNS[ci]] = max(
                widths.get(INVENTORY_COLUMNS[ci], 10), _cell_width(val, col)
            )

    for ci, col in enumerate(INVENTORY_COLUMNS):
        ws.set_column(ci, ci, min(max(widths[col] + 2, 10), 60))


def _write_cell(ws, ri, ci, value, col, fmt):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        ws.write_blank(ri, ci, None)
    elif col in _DATE_COLS and isinstance(value, date):
        ws.write_datetime(ri, ci, value, fmt["date"])
    elif col in _MONEY_COLS and isinstance(value, Decimal):
        ws.write_number(ri, ci, float(value), fmt["money"])
    elif col == "Reconciled":
        ws.write(ri, ci, value, _status_format(value, fmt))
    else:
        ws.write(ri, ci, value)


def _cell_width(value, col) -> int:
    if value is None:
        return 0
    if isinstance(value, date):
        return 10
    if isinstance(value, Decimal):
        return len(f"{value:,.2f}") + 2
    return min(len(str(value)), 80)


# ---------------------------------------------------------------------------
# Per-account transaction tabs
# ---------------------------------------------------------------------------

def _write_account_sheet(wb, sheet_name: str, g: AccountGroup, fmt: dict):
    ws = wb.add_worksheet(sheet_name)

    records = []
    for order, stmt in enumerate(g.statements):
        period = f"{stmt.period_start:%m/%d/%Y} - {stmt.period_end:%m/%d/%Y}"
        for seq, t in enumerate(stmt.transactions):
            records.append({
                "Date": t.date,
                "Type": t.tx_type,
                "Check No": t.check_no or "",
                "Description": t.description,
                "Amount": t.amount,
                "Statement Period": period,
                "Source File": t.source_file,
                "_order": (order, seq),
            })
    df = pd.DataFrame(records, columns=TX_COLUMNS + ["_order"])
    if len(df):
        # Stable: statement order (statements sorted by period_start) is
        # preserved within a date.
        df = df.sort_values(["Date"], kind="stable").drop(columns="_order")
    else:
        df = df.drop(columns="_order")

    widths = {c: len(c) for c in TX_COLUMNS}
    for ci, col in enumerate(TX_COLUMNS):
        ws.write(0, ci, col, fmt["header"])
    for ri, row in enumerate(df.itertuples(index=False), start=1):
        for ci, col in enumerate(TX_COLUMNS):
            value = getattr(row, row._fields[ci])
            if col == "Date":
                ws.write_datetime(ri, ci, value, fmt["date"])
                widths[col] = max(widths[col], 10)
            elif col == "Amount":
                ws.write_number(ri, ci, float(value), fmt["amount"])
                widths[col] = max(widths[col], len(f"{value:,.2f}") + 2)
            else:
                ws.write(ri, ci, value)
                widths[col] = max(widths[col], min(len(str(value)), 70))

    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(df), 1), len(TX_COLUMNS) - 1)
    for ci, col in enumerate(TX_COLUMNS):
        ws.set_column(ci, ci, min(max(widths[col] + 2, 10), 60))
