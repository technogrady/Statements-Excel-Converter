"""Generate sample bank-statement PDFs used by the test suite.

IMPORTANT — all data in these fixtures is SYNTHETIC. No real account
numbers, balances, transactions, merchants, names, or addresses appear
anywhere in this repository. The fixtures exist only to reproduce the
*text layout* that ``pdfplumber``'s ``page.extract_text()`` produces for
Regions and ServisFirst business-checking statements, including the
extraction quirks the parsers must survive:

* Regions: summary right-column interleave, MM/DD dates with a
  Dec→Jan year split, DAILY BALANCE SUMMARY + disclosure pages that
  must not leak into transactions.
* ServisFirst: per-page repeated headers AND a repeated customer
  mailing-address block, wrapped description continuation lines
  (including a stray account-number fragment), trailing-minus debit
  amounts, overdrawn (negative) balances, a WITHDRAWALS section that
  breaks mid-transaction across a page boundary, the three-triplet
  CHECKS table with an out-of-sequence flag, a DAILY BALANCES table,
  check-image caption pages, and multiple account blocks under one
  ``CHECKING ACCOUNTS`` banner.

The values asserted by the tests are the synthetic ground truth defined
here — chosen with round, obviously-fake numbers so the repo can be
public.

Run ``python tests/fixtures/make_fixtures.py <outdir>`` to write the
PDFs plus ``extracted_text/*.txt`` dumps of what pdfplumber actually
extracts (the dumps are what all parsing regexes were calibrated
against).
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = letter
LEFT = 54
TOP = PAGE_H - 54
LEADING = 13


def _write_pages(path: Path, pages: list[list[str]], encrypt_password: str | None = None) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    if encrypt_password:
        c.setEncrypt(encrypt_password)
    for page_lines in pages:
        c.setFont("Courier", 9)
        y = TOP
        for line in page_lines:
            c.drawString(LEFT, y, line)
            y -= LEADING
        c.showPage()
    c.save()


# ---------------------------------------------------------------------------
# Regions fixtures (synthetic)
# ---------------------------------------------------------------------------

def _regions_pages(
    *,
    account_line: str,
    label: str,
    period: str,
    summary: list[str],
    sections: list[str],
) -> list[list[str]]:
    page1 = [
        "Regions Bank",
        "Example Branch",
        "PO Box 0000",
        "Anytown, ST 00000",
        "",
        "ACME EXAMPLE LLC",
        "123 EXAMPLE ST",
        "ANYTOWN ST 00000",
        "",
        f"ACCOUNT # {account_line}",
        "Cycle 00",
        "000",
        "Page 1 of 3",
        "",
        label,
        period,
        "",
        "SUMMARY",
        *summary,
        "",
        *sections,
        "",
        "DAILY BALANCE SUMMARY",
        "Date Balance Date Balance Date Balance",
        "12/18 10,000.00 12/22 7,000.00 01/03 9,250.00",
        "12/20 8,500.00 12/31 5,000.00 01/10 9,252.00",
        "",
        "You may request account disclosures containing terms, fees, and rate information",
        "For all your banking needs, call 1-800-REGIONS (734-4667)",
    ]
    page2 = [
        "Page 2 of 3",
        "EASY STEPS TO BALANCE YOUR ACCOUNT",
        "1. Compare the checks and withdrawals listed on this statement to your register.",
        "2. List any outstanding checks 101 250.00",
        "3. Enter the ending balance shown on this statement 12/34 999.99",
        "ABBREVIATIONS: APY - Annual Percentage Yield, ATM - Automated Teller Machine",
    ]
    page3 = [
        "Page 3 of 3",
        "IN CASE OF ERRORS OR QUESTIONS ABOUT YOUR ELECTRONIC TRANSFERS",
        "Telephone us at 1-800-REGIONS or write us at the address shown above as soon as",
        "you can, if you think your statement or receipt is wrong.",
    ]
    return [page1, page2, page3]


def make_regions_dec(path: Path) -> None:
    """Regions year-split sample: period 2021-12-15 → 2022-01-14 (synthetic)."""
    pages = _regions_pages(
        account_line="xxxxxx1000",
        label="BUSINESS INTEREST CHECKING",
        period="December 15, 2021 through January 14, 2022",
        summary=[
            "Beginning Balance $10,000.00 Minimum Daily Balance $5,000.00",
            "Deposits & Credits $5,000.00 + Average Daily Balance $8,000.00",
            "Withdrawals $5,750.00 - Annual Percentage Yield Earned 0.01%",
            "Checks $0.00 - Interest Paid This Year $2.00",
            "Fees $0.00 -",
            "Net Interest Earned $2.00 +",
            "Ending Balance $9,252.00",
        ],
        sections=[
            "DEPOSITS & CREDITS",
            "01/03 Sweep from Investment Acct 5,000.00",
            "Total Deposits & Credits $5,000.00",
            "",
            "INTEREST",
            "01/10 Net Interest Earned This Period 2.00",
            "Total Interest $2.00",
            "",
            "WITHDRAWALS",
            "12/18 Online Transfer to LOC 0000 1,000.00",
            "12/20 Online Transfer to LOC 0000 1,500.00",
            "12/22 Online Transfer to LOC 0000 500.00",
            "12/22 Wire Transfer Out Example Vendor 750.00",
            "12/31 Online Transfer to LOC 0000 2,000.00",
            "Total Withdrawals $5,750.00",
        ],
    )
    _write_pages(path, pages)


def make_regions_jan(path: Path) -> None:
    """Follow-on Regions month: chains from the Dec statement (opening 9,252.00),
    carries a different product label to exercise label-variance reporting."""
    pages = _regions_pages(
        account_line="xxxxxx1000",
        label="BUSINESS CHECKINGS",
        period="January 15, 2022 through February 14, 2022",
        summary=[
            "Beginning Balance $9,252.00 Minimum Daily Balance $8,750.00",
            "Deposits & Credits $1,000.00 + Average Daily Balance $9,500.00",
            "Withdrawals $500.00 - Annual Percentage Yield Earned 0.01%",
            "Checks $0.00 - Interest Paid This Year $1.00",
            "Fees $0.00 -",
            "Net Interest Earned $1.00 +",
            "Ending Balance $9,753.00",
        ],
        sections=[
            "DEPOSITS & CREDITS",
            "01/20 Deposit 1,000.00",
            "Total Deposits & Credits $1,000.00",
            "",
            "INTEREST",
            "02/10 Net Interest Earned This Period 1.00",
            "Total Interest $1.00",
            "",
            "WITHDRAWALS",
            "02/01 Online Transfer to LOC 0000 500.00",
            "Total Withdrawals $500.00",
        ],
    )
    _write_pages(path, pages)


def make_regions_apr(path: Path) -> None:
    """Regions month after a coverage gap (Feb 14 → Apr 15 leaves March
    uncovered); opening balance deliberately does not extend the Feb closing."""
    pages = _regions_pages(
        account_line="xxxxxx1000",
        label="BUSINESS CHECKINGS",
        period="April 15, 2022 through May 14, 2022",
        summary=[
            "Beginning Balance $12,000.00 Minimum Daily Balance $11,000.00",
            "Deposits & Credits $0.00 + Average Daily Balance $11,500.00",
            "Withdrawals $1,000.00 - Annual Percentage Yield Earned 0.00%",
            "Checks $0.00 - Interest Paid This Year $0.00",
            "Fees $0.00 -",
            "Net Interest Earned $0.00 +",
            "Ending Balance $11,000.00",
        ],
        sections=[
            "WITHDRAWALS",
            "04/20 Online Transfer to LOC 0000 1,000.00",
            "Total Withdrawals $1,000.00",
        ],
    )
    _write_pages(path, pages)


# ---------------------------------------------------------------------------
# ServisFirst fixtures (synthetic)
# ---------------------------------------------------------------------------

def _sf_header(page_no: int) -> list[str]:
    return [
        "ServisFirst Bank",
        "0000 Example Place",
        "Anytown, ST 00000",
        "(000) 000-0000",
        f"Date 8/30/22 Page {page_no}",
        "Primary Acct. XXXXXXXXXXXX5678",
        "",
    ]


# The customer mailing-address block that reprints under the header on every
# page of a real statement — must be stripped so it can't glue into a
# wrapped transaction description.
_SF_ADDRESS = ["ACME EXAMPLE LLC", "123 EXAMPLE ST", "ANYTOWN ST 00000"]
_SF_FOOTER = ["MEMBER FDIC", "NOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION"]


def make_servisfirst(path: Path) -> None:
    """ServisFirst sample: period 2022-07-30 → 2022-09-02 (synthetic).

    9 deposits totaling 19,000.00; 9 non-check debit rows and 13 checks
    (5001–5014, no 5007, 5010 flagged out of sequence) totaling
    12,450.00 across 22 Checks/Debits. 20,000.00 + 19,000.00 −
    12,450.00 = 26,550.00 — reconciles exactly.
    """
    page1 = [
        *_sf_header(1),
        *_SF_ADDRESS,
        "",
        "C H E C K I N G   A C C O U N T S",
        "",
        "BUSINESS CHECKING",
        "Account Number XXXXXXXXXXXX5678 Statement Dates 7/30/22 thru 9/02/22",
        "Previous Balance 20,000.00 Days in the statement period 35",
        "9 Deposits/Credits 19,000.00 Average Ledger 22,000.00",
        "22 Checks/Debits 12,450.00 Average Collected 21,500.00",
        "Service Charge .00",
        "Interest Paid .00",
        "Current Balance 26,550.00",
        "",
        "DEPOSITS AND OTHER CREDITS",
        "Date Description Amount",
        "7/30 From LOC 10000,To DDA 20000 1,000.00",
        "80",
        "8/06 From LOC 10000,To DDA 20000 2,000.00",
        "80",
        "8/08 REMOTE DEPOSIT 3,000.00",
        "8/12 From LOC 10000,To DDA 20000 4,000.00",
        "80",
        "8/15 REMOTE DEPOSIT 500.00",
        "8/19 From LOC 10000,To DDA 20000 1,500.00",
        "80",
        "8/22 From LOC 10000,To DDA 20000 2,500.00",
        "80",
        "8/27 From LOC 10000,To DDA 20000 3,500.00",
        "80",
        "8/29 From LOC 10000,To DDA 20000 1,000.00",
        "80",
        "",
        "WITHDRAWALS AND DEBITS",
        "Date Description Amount",
        "7/31 ACH ACME SUPPLY PAYMENTS 1,000.00-",
        "100001",
        "8/01 DBT CRD 0001 07/31/22 00000000 50.00-",
        "SAMPLE MERCHANT ANYTOWN ST",
        "C#0001",
        "8/05 EXAMPLE UTILITY COOP 200.00-",
        "UTILITY PMT",
        "8/11 TRANSFER TO LOAN 20000 300.00-",
        "8/15 ACH ACME SUPPLY PAYMENTS 400.00-",
        "100002",
        "8/18 DBT CRD 0002 08/17/22 00000000 25.00-",
        "GENERIC STORE #001",
        *_SF_FOOTER,
    ]
    page2 = [
        *_sf_header(2),
        *_SF_ADDRESS,  # mailing block reprints on the continuation page
        "",
        "BUSINESS CHECKING XXXXXXXXXXXX5678 (Continued)",
        "",
        "WITHDRAWALS AND DEBITS",
        "Date Description Amount",
        "ANYTOWN ST C#0002",  # continuation of the last debit on page 1
        "8/25 ACH EXAMPLE CARD PMT 500.00-",
        "W0001",
        "8/28 EXAMPLE POWER CO 100.00-",
        "8/29 UTILITY DIRECT DEBIT 75.00-",
        "",
        "CHECKS",
        "Date Check No Amount Date Check No Amount Date Check No Amount",
        "8/02 5001 100.00 8/09 5006 600.00 8/23 5012 1,200.00",
        "7/31 5002 200.00 8/12 5010* 1,000.00 8/26 5013 1,300.00",
        "7/31 5003 300.00 8/14 5011 1,100.00 8/28 5014 1,400.00",
        "8/05 5004 400.00 8/16 5008 800.00",
        "8/07 5005 500.00 8/20 5009 900.00",
        "* Indicates Serial Number Out of Sequence",
        "",
        "DAILY BALANCES",
        "Date Balance Date Balance Date Balance",
        "7/30 21,000.00 8/11 19,000.00 8/23 24,000.00",
        "8/01 20,000.00 8/15 18,500.00 8/28 25,000.00",
        "8/08 22,000.00 8/20 17,000.00 9/02 26,550.00",
        *_SF_FOOTER,
    ]
    page3 = [
        *_sf_header(3),
        *_SF_ADDRESS,
        "",
        "BUSINESS CHECKING XXXXXXXXXXXX5678 (Continued)",
        "",
        "Check 5001 Amount $100.00 Date 8/2/2022",
        "Check 5002 Amount $200.00 Date 7/31/2022",
        "Check 5003 Amount $300.00 Date 7/31/2022",
        "Check 5004 Amount $400.00 Date 8/5/2022",
        "Check 5005 Amount $500.00 Date 8/7/2022",
        "Check 5006 Amount $600.00 Date 8/9/2022",
        "Amount $3,000.00 Date 8/8/2022",
        "Amount $500.00 Date 8/15/2022",
        *_SF_FOOTER,
    ]
    page4 = [
        *_sf_header(4),
        *_SF_ADDRESS,
        "",
        "BUSINESS CHECKING XXXXXXXXXXXX5678 (Continued)",
        "",
        "Check 5008 Amount $800.00 Date 8/16/2022",
        "Check 5009 Amount $900.00 Date 8/20/2022",
        "Check 5010 Amount $1,000.00 Date 8/12/2022",
        "Check 5011 Amount $1,100.00 Date 8/14/2022",
        "Check 5012 Amount $1,200.00 Date 8/23/2022",
        "Check 5013 Amount $1,300.00 Date 8/26/2022",
        "Check 5014 Amount $1,400.00 Date 8/28/2022",
        *_SF_FOOTER,
    ]
    _write_pages(path, [page1, page2, page3, page4])


def make_servisfirst_multi_account(path: Path) -> None:
    """Two account blocks under one CHECKING ACCOUNTS banner — exercises
    per-account label attribution (each block keeps its own label)."""
    page = [
        *_sf_header(1),
        *_SF_ADDRESS,
        "",
        "C H E C K I N G   A C C O U N T S",
        "",
        "BUSINESS CHECKING",
        "Account Number XXXXXXXXXXXX1111 Statement Dates 8/01/22 thru 8/31/22",
        "Previous Balance 1,000.00 Days in the statement period 31",
        "1 Deposits/Credits 500.00 Average Ledger 1,200.00",
        "0 Checks/Debits .00 Average Collected 1,100.00",
        "Current Balance 1,500.00",
        "",
        "DEPOSITS AND OTHER CREDITS",
        "Date Description Amount",
        "8/05 Deposit 500.00",
        "",
        "BUSINESS SAVINGS",
        "Account Number XXXXXXXXXXXX2222 Statement Dates 8/01/22 thru 8/31/22",
        "Previous Balance 5,000.00 Days in the statement period 31",
        "1 Deposits/Credits 1,000.00 Average Ledger 5,500.00",
        "0 Checks/Debits .00 Average Collected 5,400.00",
        "Current Balance 6,000.00",
        "",
        "DEPOSITS AND OTHER CREDITS",
        "Date Description Amount",
        "8/10 Deposit 1,000.00",
        *_SF_FOOTER,
    ]
    _write_pages(path, [page])


def make_servisfirst_overdrawn(path: Path) -> None:
    """Overdrawn account: closing balance prints with a trailing minus and
    must parse negative (500.00 − 700.00 = −200.00)."""
    page = [
        *_sf_header(1),
        *_SF_ADDRESS,
        "",
        "C H E C K I N G   A C C O U N T S",
        "",
        "BUSINESS CHECKING",
        "Account Number XXXXXXXXXXXX3333 Statement Dates 8/01/22 thru 8/31/22",
        "Previous Balance 500.00 Days in the statement period 31",
        "0 Deposits/Credits .00 Average Ledger 300.00",
        "1 Checks/Debits 700.00 Average Collected 200.00",
        "Current Balance 200.00-",
        "",
        "WITHDRAWALS AND DEBITS",
        "Date Description Amount",
        "8/05 EXAMPLE VENDOR PAYMENT 700.00-",
        *_SF_FOOTER,
    ]
    _write_pages(path, [page])


# ---------------------------------------------------------------------------
# Negative-path fixtures
# ---------------------------------------------------------------------------

def make_unrecognized_bank(path: Path) -> None:
    _write_pages(
        path,
        [[
            "Example National Bank of Testing",
            "STATEMENT OF ACCOUNT",
            "Account 00000 Period 01/01/2022 - 01/31/2022",
        ]],
    )


def make_no_text(path: Path) -> None:
    """A PDF with no extractable text (simulates a scanned statement)."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.rect(100, 100, 400, 600, fill=1)
    c.showPage()
    c.save()


def make_encrypted(path: Path) -> None:
    _write_pages(
        path,
        [["Regions Bank", "This content is password protected."]],
        encrypt_password="secret",
    )


ALL_FIXTURES = {
    "regions_checking_2022-01.pdf": make_regions_dec,
    "regions_checking_2022-02.pdf": make_regions_jan,
    "regions_checking_2022-05.pdf": make_regions_apr,
    "servisfirst_checking_2022-09.pdf": make_servisfirst,
    "servisfirst_multi_account.pdf": make_servisfirst_multi_account,
    "servisfirst_overdrawn.pdf": make_servisfirst_overdrawn,
    "regions_checking_2022-01_redownload.pdf": make_regions_dec,
    "unknown_bank.pdf": make_unrecognized_bank,
    "scanned_image_only.pdf": make_no_text,
    "password_protected.pdf": make_encrypted,
}

# Fixtures included in the default end-to-end consolidation run (excludes the
# standalone multi-account / overdrawn cases, which have their own unit tests).
END_TO_END_FIXTURES = [
    "regions_checking_2022-01.pdf",
    "regions_checking_2022-02.pdf",
    "regions_checking_2022-05.pdf",
    "servisfirst_checking_2022-09.pdf",
    "regions_checking_2022-01_redownload.pdf",
    "unknown_bank.pdf",
    "scanned_image_only.pdf",
    "password_protected.pdf",
]


def build_all(outdir: Path) -> dict[str, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, builder in ALL_FIXTURES.items():
        p = outdir / name
        builder(p)
        paths[name] = p
    return paths


def build_end_to_end(outdir: Path) -> dict[str, Path]:
    """Only the fixtures that belong in the default folder-consolidation run."""
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in END_TO_END_FIXTURES:
        p = outdir / name
        ALL_FIXTURES[name](p)
        paths[name] = p
    return paths


def dump_extracted_text(pdf_dir: Path, txt_dir: Path) -> None:
    """Dump pdfplumber's page.extract_text() for every fixture — the raw
    material the parsing regexes are calibrated against."""
    import pdfplumber

    txt_dir.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        out = txt_dir / (pdf.stem + ".txt")
        try:
            with pdfplumber.open(pdf) as doc:
                chunks = []
                for i, page in enumerate(doc.pages, 1):
                    chunks.append(f"===== page {i} =====")
                    chunks.append(page.extract_text() or "(no text)")
                out.write_text("\n".join(chunks) + "\n")
        except Exception as exc:  # encrypted fixture
            out.write_text(f"(extraction failed: {type(exc).__name__}: {exc})\n")


if __name__ == "__main__":
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "generated"
    paths = build_all(outdir)
    dump_extracted_text(outdir, outdir / "extracted_text")
    print(f"wrote {len(paths)} fixture PDFs to {outdir}")
