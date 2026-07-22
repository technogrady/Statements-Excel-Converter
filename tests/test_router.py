from statement_parsers import detect_bank, parse_pdf, regions, servisfirst
from statement_parsers.base import (
    STATUS_ENCRYPTED,
    STATUS_NO_TEXT,
    STATUS_OK,
    STATUS_UNRECOGNIZED,
)


class TestDetection:
    def test_regions_by_name(self):
        assert detect_bank("... Regions Bank ...") is regions

    def test_regions_by_phone(self):
        assert detect_bank("call 1-800-REGIONS today") is regions

    def test_servisfirst(self):
        assert detect_bank("ServisFirst Bank member FDIC") is servisfirst

    def test_servis1st_variant(self):
        assert detect_bank("Servis1st Bank") is servisfirst

    def test_servisfirst_by_disclosure_when_wordmark_is_image_only(self):
        # Some ServisFirst PDFs render the wordmark as an image (no extractable
        # "ServisFirst" text); the reverse-side disclosure footer recovers them.
        text = (
            "P.O. Box 1508 Birmingham, AL 35201 MEMBER FDIC "
            "NOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION"
        )
        assert detect_bank(text) is servisfirst

    def test_regions_wins_over_shared_disclosure(self):
        # A Regions statement is still routed to Regions even if it happens to
        # carry a similar disclosure footer (Regions is matched first).
        text = "Regions Bank\nNOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION"
        assert detect_bank(text) is regions

    def test_case_insensitive(self):
        assert detect_bank("REGIONS BANK") is regions
        assert detect_bank("servisfirst bank") is servisfirst

    def test_unknown_is_none(self):
        assert detect_bank("Some Other Bank NA") is None


class TestParsePdfStatuses:
    def test_ok(self, fixture_dir):
        r = parse_pdf(fixture_dir / "regions_checking_2022-01.pdf")
        assert r.status == STATUS_OK

    def test_unrecognized_with_hint(self, fixture_dir):
        r = parse_pdf(fixture_dir / "unknown_bank.pdf")
        assert r.status == STATUS_UNRECOGNIZED
        assert "Example National Bank of Testing" in r.detail
        assert len(r.detail) <= 100

    def test_no_text_flagged_as_possible_scan(self, fixture_dir):
        r = parse_pdf(fixture_dir / "scanned_image_only.pdf")
        assert r.status == STATUS_NO_TEXT
        assert "scan" in r.detail

    def test_encrypted(self, fixture_dir):
        r = parse_pdf(fixture_dir / "password_protected.pdf")
        assert r.status == STATUS_ENCRYPTED

    def test_never_raises_on_garbage_file(self, tmp_path):
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"%PDF-1.4 garbage" + b"\x00" * 64)
        r = parse_pdf(bad)  # must not raise
        assert r.status != STATUS_OK


# A minimal but reconciling single-account ServisFirst statement, used to stand
# in for text that PyMuPDF recovers when pdfminer can't decode the text layer.
_RICH_SERVISFIRST = """ServisFirst Bank
C H E C K I N G A C C O U N T S
BUSINESS CHECKING
Account Number XXXXXXXXXXXX5678 Statement Dates 3/01/25 thru 3/31/25
Previous Balance 100.00 Days in the Statement Period 30
Current Balance 150.00
DEPOSITS AND OTHER CREDITS
Date Description Amount
3/05 REMOTE DEPOSIT 50.00
MEMBER FDIC
NOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION
"""


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePDF:
    def __init__(self, pages):
        self.pages = [_FakePage(p) for p in pages]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPyMuPDFFallback:
    """parse_pdf falls back to PyMuPDF when pdfplumber under-extracts, and
    leaves the pdfplumber-happy path completely untouched."""

    def test_fallback_recovers_undecodable_text(self, monkeypatch):
        import pdfplumber

        import statement_parsers as sp

        # pdfplumber only recovers a routable footer (body undecodable) -> the
        # ServisFirst parser is chosen but fails with 'no account blocks found'.
        monkeypatch.setattr(
            pdfplumber, "open",
            lambda *a, **k: _FakePDF(["NOTICE: SEE REVERSE SIDE FOR IMPORTANT INFORMATION"]),
        )
        # PyMuPDF recovers the full statement.
        monkeypatch.setattr(sp, "_pymupdf_pages", lambda path: [_RICH_SERVISFIRST])

        r = sp.parse_pdf("undecodable.pdf")
        assert r.status == STATUS_OK
        assert len(r.statements) == 1
        assert r.statements[0].bank == "ServisFirst"
        assert r.statements[0].reconciled is True

    def test_fallback_preferred_when_pdfplumber_parses_but_does_not_reconcile(
        self, monkeypatch
    ):
        import pdfplumber

        import statement_parsers as sp

        # pdfplumber parses but misses the deposit -> parses OK yet does NOT
        # reconcile (opening 100 + 0 != closing 150).
        partial = """ServisFirst Bank
BUSINESS CHECKING
Account Number XXXXXXXXXXXX5678 Statement Dates 3/01/25 thru 3/31/25
Previous Balance 100.00
Current Balance 150.00
DEPOSITS AND OTHER CREDITS
Date Description Amount
MEMBER FDIC
"""
        monkeypatch.setattr(pdfplumber, "open", lambda *a, **k: _FakePDF([partial]))
        monkeypatch.setattr(sp, "_pymupdf_pages", lambda path: [_RICH_SERVISFIRST])

        r = sp.parse_pdf("partial.pdf")
        assert r.status == STATUS_OK
        assert r.statements[0].reconciled is True  # the PyMuPDF version won

    def test_fallback_not_used_when_pdfplumber_succeeds(self, monkeypatch):
        import pdfplumber

        import statement_parsers as sp

        monkeypatch.setattr(
            pdfplumber, "open", lambda *a, **k: _FakePDF([_RICH_SERVISFIRST])
        )

        def _boom(path):
            raise AssertionError("PyMuPDF fallback must not run when pdfplumber wins")

        monkeypatch.setattr(sp, "_pymupdf_pages", _boom)

        r = sp.parse_pdf("clean.pdf")
        assert r.status == STATUS_OK
        assert r.statements[0].reconciled is True
