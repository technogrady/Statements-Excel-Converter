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
