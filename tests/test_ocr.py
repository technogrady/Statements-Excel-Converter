"""OCR fallback tests.

Most of these fake the engine, so the suite never needs Tesseract
installed. ``TestRealOCR`` exercises the genuine render → tesseract →
parse path and is skipped when the binary isn't present.
"""
import os
import subprocess
from pathlib import Path

import pytest

import statement_parsers as sp
from statement_parsers import ocr
from statement_parsers.base import STATUS_NO_TEXT, STATUS_OK, STATUS_PARSE_ERROR

# A complete, reconciling Wells Fargo savings statement — stands in for what
# OCR recovers from a scanned page.
_SCANNED_TEXT = [
    """Business Market Rate Savings
January 31, 2022 Page 1 of 2
Online: wellsfargo.com/biz
Statement period activity summary Account number: 0000001234
Beginning balance on 1/1 $1,000.00 EXAMPLE COMPANY INC
Deposits/Credits 2,000.00
Withdrawals/Debits - 500.00
Ending balance on 1/31 $2,500.00
""",
    """January 31, 2022 Page 2 of 2
Transaction history
Date Description Credits Debits balance
1/05 Online Transfer From Example Company Inc Ref #Ab0000001 2,000.00 3,000.00
1/18 Online Transfer to Example Company Inc Ref #Ab0000002 500.00 2,500.00
Totals $2,000.00 $500.00
""",
]


class TestFindTesseract:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = tmp_path / "tesseract"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setenv(ocr.TESSERACT_ENV, str(fake))
        assert ocr.find_tesseract() == str(fake)

    def test_env_override_pointing_at_nothing_is_not_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ocr.TESSERACT_ENV, str(tmp_path / "nope"))
        assert ocr.find_tesseract() is None

    def test_found_on_path(self, monkeypatch):
        monkeypatch.delenv(ocr.TESSERACT_ENV, raising=False)
        monkeypatch.setattr(ocr.shutil, "which",
                            lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)
        assert ocr.find_tesseract() == "/usr/bin/tesseract"


class TestAvailability:
    def test_reports_missing_engine_with_install_hint(self, monkeypatch):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
        ok, reason = ocr.availability()
        assert not ok
        assert "winget" in reason and "brew" in reason

    def test_ok_when_engine_present(self, monkeypatch):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: "/usr/bin/tesseract")
        assert ocr.availability() == (True, "")


class TestOcrPages:
    def test_declines_when_engine_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
        assert ocr.ocr_pages(tmp_path / "whatever.pdf") is None

    def test_renders_every_page_and_returns_text(self, monkeypatch, fixture_dir):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: "/usr/bin/tesseract")
        seen = []

        def fake_run(exe, image):
            seen.append(Path(image).name)
            return f"text of {Path(image).name}"

        monkeypatch.setattr(ocr, "_run_tesseract", fake_run)
        pages = ocr.ocr_pages(fixture_dir / "wellsfargo_savings_2022-01.pdf")
        assert pages == ["text of page1.png", "text of page2.png"]
        assert seen == ["page1.png", "page2.png"]

    def test_uses_block_segmentation_mode(self, monkeypatch, tmp_path):
        """psm 6 keeps each printed row on one line; tesseract's default mode
        would split the transaction table into per-column runs."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, b"ok", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ocr._run_tesseract("/usr/bin/tesseract", tmp_path / "p.png")
        assert "--psm" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--psm") + 1] == "6"
        assert "preserve_interword_spaces=1" in captured["cmd"]

    def test_engine_failure_declines_instead_of_raising(self, monkeypatch, fixture_dir):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: "/usr/bin/tesseract")

        def boom(exe, image):
            raise subprocess.CalledProcessError(1, "tesseract")

        monkeypatch.setattr(ocr, "_run_tesseract", boom)
        assert ocr.ocr_pages(fixture_dir / "wellsfargo_savings_2022-01.pdf") is None

    def test_unopenable_pdf_declines(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ocr, "find_tesseract", lambda: "/usr/bin/tesseract")
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"%PDF-1.4 garbage" + b"\x00" * 64)
        assert ocr.ocr_pages(bad) is None


class TestParsePdfOcrStage:
    def test_scan_is_no_text_without_the_flag(self, fixture_dir, monkeypatch):
        def _boom(path):
            raise AssertionError("OCR must not run unless asked for")

        monkeypatch.setattr(sp, "_ocr_pages", _boom)
        r = sp.parse_pdf(fixture_dir / "scanned_image_only.pdf")
        assert r.status == STATUS_NO_TEXT
        assert r.via_ocr is False
        assert "--ocr" in r.detail

    def test_scan_is_recovered_with_the_flag(self, fixture_dir, monkeypatch):
        monkeypatch.setattr(sp, "_ocr_pages", lambda path: _SCANNED_TEXT)
        r = sp.parse_pdf(fixture_dir / "scanned_image_only.pdf", ocr=True)
        assert r.status == STATUS_OK
        assert r.via_ocr is True
        stmt = r.statements[0]
        assert stmt.bank == "Wells Fargo"
        assert stmt.reconciled
        assert len(stmt.transactions) == 2

    def test_ocr_statements_carry_a_spot_check_note(self, fixture_dir, monkeypatch):
        monkeypatch.setattr(sp, "_ocr_pages", lambda path: _SCANNED_TEXT)
        r = sp.parse_pdf(fixture_dir / "scanned_image_only.pdf", ocr=True)
        assert r.statements[0].notes[0] == sp.OCR_NOTE
        assert "machine-read" in r.statements[0].notes[0]

    def test_unavailable_engine_leaves_the_scan_reported_as_a_scan(
        self, fixture_dir, monkeypatch
    ):
        monkeypatch.setattr(sp, "_ocr_pages", lambda path: None)
        r = sp.parse_pdf(fixture_dir / "scanned_image_only.pdf", ocr=True)
        assert r.status == STATUS_NO_TEXT
        assert r.via_ocr is False

    def test_misread_ocr_is_rejected_rather_than_imported(self, fixture_dir, monkeypatch):
        """A digit OCR got wrong breaks reconciliation. Importing those numbers
        would be worse than reporting the file unread, so it is declined."""
        misread = [
            _SCANNED_TEXT[0],
            _SCANNED_TEXT[1].replace("2,000.00 3,000.00", "9,000.00 3,000.00"),
        ]
        monkeypatch.setattr(sp, "_ocr_pages", lambda path: misread)
        r = sp.parse_pdf(fixture_dir / "scanned_image_only.pdf", ocr=True)
        assert r.status == STATUS_NO_TEXT
        assert r.via_ocr is False

    def test_ocr_never_overrides_a_reconciling_text_parse(self, fixture_dir, monkeypatch):
        def _boom(path):
            raise AssertionError("OCR must not run when the text layer parses")

        monkeypatch.setattr(sp, "_ocr_pages", _boom)
        r = sp.parse_pdf(fixture_dir / "wellsfargo_savings_2022-01.pdf", ocr=True)
        assert r.status == STATUS_OK
        assert r.via_ocr is False

    def test_unreconciled_text_parse_is_not_re_read_by_ocr(self, tmp_path, monkeypatch):
        """A statement that parses but doesn't balance is a real discrepancy to
        report — not a scan. OCR-ing it would only cost minutes."""
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        path = tmp_path / "off_by_one.pdf"
        c = canvas.Canvas(str(path), pagesize=letter)
        c.setFont("Courier", 9)
        y = 700
        for line in [
            "Business Market Rate Savings",
            "January 31, 2022 Page 1 of 1",
            "Online: wellsfargo.com/biz",
            "Statement period activity summary Account number: 0000001234",
            "Beginning balance on 1/1 $1,000.00",
            "Ending balance on 1/31 $9,999.00",
            "Transaction history",
            "Date Description Credits Debits balance",
            "1/05 Deposit 100.00 1,100.00",
            "Totals $100.00 $0.00",
        ]:
            c.drawString(54, y, line)
            y -= 13
        c.showPage()
        c.save()

        def _boom(path):
            raise AssertionError("OCR must not re-read a file that parsed")

        monkeypatch.setattr(sp, "_ocr_pages", _boom)
        r = sp.parse_pdf(path, ocr=True)
        assert r.status == STATUS_OK
        assert not r.statements[0].reconciled

    def test_part_rescanned_file_is_re_read(self, monkeypatch):
        """A file where one page lost its text layer parses 'successfully' while
        silently missing that page's transactions — so it does get OCR'd."""
        import pdfplumber

        from tests.test_router import _FakePDF  # noqa: PLC0415

        monkeypatch.setattr(
            pdfplumber, "open", lambda *a, **k: _FakePDF([_SCANNED_TEXT[0], ""])
        )
        monkeypatch.setattr(sp, "_pymupdf_pages", lambda path: None)
        monkeypatch.setattr(sp, "_ocr_pages", lambda path: _SCANNED_TEXT)
        r = sp.parse_pdf("half_scanned.pdf", ocr=True)
        assert r.via_ocr is True
        assert len(r.statements[0].transactions) == 2


class TestCli:
    def test_ocr_flag_is_passed_through(self, fixture_dir, tmp_path, monkeypatch):
        import parse_statements

        seen = []
        monkeypatch.setattr(parse_statements, "ocr_availability", lambda: (True, ""))
        real = parse_statements.parse_pdf
        monkeypatch.setattr(
            parse_statements, "parse_pdf",
            lambda path, ocr=False: seen.append(ocr) or real(path, ocr=ocr),
        )
        rc = parse_statements.main(
            [str(fixture_dir), "-o", str(tmp_path / "out.xlsx"), "--ocr"]
        )
        assert rc == 0
        assert seen and all(seen)

    def test_missing_engine_warns_and_continues(self, fixture_dir, tmp_path, monkeypatch, capsys):
        import parse_statements

        monkeypatch.setattr(
            parse_statements, "ocr_availability", lambda: (False, "no tesseract here")
        )
        seen = []
        real = parse_statements.parse_pdf
        monkeypatch.setattr(
            parse_statements, "parse_pdf",
            lambda path, ocr=False: seen.append(ocr) or real(path, ocr=ocr),
        )
        rc = parse_statements.main(
            [str(fixture_dir), "-o", str(tmp_path / "out.xlsx"), "--ocr"]
        )
        assert rc == 0
        assert not any(seen)  # fell back to no OCR rather than failing the run
        assert "no tesseract here" in capsys.readouterr().err

    def test_scan_without_the_flag_suggests_it(self, fixture_dir, tmp_path, capsys):
        import parse_statements

        parse_statements.main([str(fixture_dir), "-o", str(tmp_path / "out.xlsx")])
        assert "--ocr" in capsys.readouterr().out


@pytest.mark.skipif(
    ocr.find_tesseract() is None,
    reason="Tesseract OCR is not installed on this machine",
)
class TestRealOCR:
    """The genuine path: rasterize a statement into an image-only PDF (exactly
    what a scanner produces), then render → tesseract → parse it for real."""

    @staticmethod
    def _flatten_to_image_pdf(src: Path, dst: Path) -> None:
        import fitz

        with fitz.open(src) as doc, fitz.open() as out:
            for index in range(doc.page_count):
                page = doc[index]
                pixmap = page.get_pixmap(dpi=200)
                new_page = out.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(new_page.rect, pixmap=pixmap)
            out.save(dst)

    def test_scanned_statement_is_read_and_reconciles(self, fixture_dir, tmp_path):
        scanned = tmp_path / "wellsfargo_scanned.pdf"
        self._flatten_to_image_pdf(fixture_dir / "wellsfargo_savings_2022-01.pdf", scanned)

        # Sanity: the flattened file really has no text layer.
        assert sp.parse_pdf(scanned).status == STATUS_NO_TEXT

        r = sp.parse_pdf(scanned, ocr=True)
        assert r.status == STATUS_OK, r.detail
        assert r.via_ocr is True
        stmt = r.statements[0]
        assert stmt.reconciled
        assert stmt.bank == "Wells Fargo"
        assert len(stmt.transactions) == 8
        assert str(stmt.opening_balance) == "1000.00"
        assert str(stmt.closing_balance) == "5500.16"
