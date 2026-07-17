# Bank Statement → Excel Consolidation Tool

Parses a folder of bank statement PDFs (digitally generated text — not
scans) and produces a single Excel workbook (`Bank_Statements.xlsx`)
with:

1. **One worksheet per bank account** — every transaction from every
   statement for that account, sorted by date, statement-level
   deduplicated.
2. **An `Inventory` worksheet (first tab)** — one row per statement:
   what it is, the period it covers, balances, whether it reconciled to
   the penny, plus a per-account **Coverage Summary** (labels used over
   time, balance-chain status, missing months).

Supported banks: **Regions Bank** and **ServisFirst Bank**. Adding a
third bank is one new parser module (see [Extensibility](#extensibility)).

## Windows setup (easy — start here)

You need two things once: **Git** (to download the tool) and **Python**
(to run it). The install script below handles Python for you.

**1. Install Git.** Open **PowerShell** (press `Start`, type `powershell`,
press Enter) and run:

```powershell
winget install --id Git.Git -e
```

Close that window and open a **new** PowerShell window afterwards so Windows
picks up Git.

**2. Download the tool.** Pick a folder to keep it in (your home folder is
fine) and run:

```powershell
cd ~
git clone https://github.com/technogrady/statements-excel-converter.git
cd statements-excel-converter
```

**3. Run the installer.** Just run:

```powershell
.\install.bat
```

This installs Python if it's missing, sets up a self-contained environment
inside the folder (in `.venv`) and installs the tool's packages there — your
system-wide Python is left untouched — and adds this folder to your PATH so
you can run it from anywhere. When it finishes, **close the window and open a
new one**.

**4. Use it.** In the new window, point it at a folder full of statement
PDFs:

```powershell
statements "C:\Users\you\Downloads\statements"
```

That writes `Bank_Statements.xlsx` into whatever folder you're currently in.
To choose where the workbook goes, add `-o`:

```powershell
statements "C:\Users\you\Downloads\statements" -o "C:\Users\you\Desktop\Book.xlsx"
```

> **Prefer clicking to typing?** After step 2 you can just double-click
> `install.bat` in File Explorer to do step 3.

To update the tool later, `cd` back into the folder and run `git pull`.

## Usage (any platform)

```bash
pip install -r requirements.txt
python parse_statements.py <input_folder> [-o output.xlsx]
```

The folder is searched recursively for `*.pdf` (case-insensitive). One
bad PDF never aborts the run — encrypted, unrecognized, scanned-image,
or unparsable files become flagged Inventory rows, and a run summary is
printed to stdout.

## What the tool checks for you

* **Reconciliation (per statement)** — `opening + Σ(transactions) ==
  closing`, exactly, using `Decimal` throughout (never float). Failures
  show as `FAILED (Δ $x.xx)` on the Inventory tab; parsed counts and
  totals are also cross-checked against the statement's own summary
  figures (Regions section totals; ServisFirst `9 Deposits/Credits` /
  `22 Checks/Debits` declarations, check-image captions).
* **Balance chaining (per account)** — each statement's closing balance
  must equal the next statement's opening balance. Chains that link
  across a product-name change prove "renamed account, same account";
  adjacent periods that don't link are flagged `⚠ Balance
  discontinuity`. For masked account numbers (ServisFirst), statements
  that partition into two self-consistent parallel chains are split
  into `_a`/`_b` sheets and flagged `⚠ Possible second account`.
* **Missing months** — any month between an account's earliest and
  latest coverage not touched by any statement period.
* **Deduplication** — statements with the same (bank, account last4,
  period) are counted once; re-downloads are marked `DUPLICATE of
  <file>` on the Inventory tab. Statements printing *different full
  account numbers* are never merged, even with identical last4s.
  Individual transactions are never deduped across statements —
  recurring identical transactions are legitimate.

## Architecture

```
parse_statements.py        # CLI entry point + run summary
statement_parsers/
    __init__.py            # detect_bank(text) router + per-file orchestration
    base.py                # shared dataclasses (Decimal money) + year inference
    regions.py             # Regions Bank parser
    servisfirst.py         # ServisFirst Bank parser
consolidation.py           # dedup, account grouping, balance chaining,
                           # label variance, missing-month detection
excel_writer.py            # workbook generation (xlsxwriter)
```

`consolidation.py` sits between the parsers (one statement at a time)
and the writer (formatting only): it reasons about the collection as a
whole.

Money is `Decimal` end to end; floats appear only at the cell-write
boundary. Transaction dates printed as `MM/DD` are resolved against the
statement period, correctly handling periods that span Dec→Jan.

## Extensibility

A third bank = one new module in `statement_parsers/` implementing:

```python
BANK = "NewBank"
def matches(page1_text: str) -> bool: ...
def parse(pages: list[str], filename: str) -> list[ParsedStatement]: ...
```

registered by appending it to `PARSERS` in
`statement_parsers/__init__.py`. Nothing else changes.

## Tests & calibration

The original statement samples can't be committed, so
`tests/fixtures/make_fixtures.py` synthesizes PDFs that reproduce their
pdfplumber extraction layout — including the quirks the parsers must
survive (wrapped description fragments, trailing-minus amounts,
sections breaking mid-transaction across pages, three-triplet CHECKS
rows, check-image caption pages, interleaved summary columns).
`tests/fixtures/extracted_text/` holds the raw `page.extract_text()`
dumps the parsing regexes were calibrated against — per the project
rule: build parsing against actual extracted text, not the visual PDF.

```bash
python -m pytest tests/
```

The suite asserts the engagement's known-correct ground truth (balances,
every transaction, year splits, out-of-sequence check flags, exact
reconciliation) end-to-end through real generated PDFs, plus workbook
structure via openpyxl.
