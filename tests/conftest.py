import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from fixtures.make_fixtures import ALL_FIXTURES, build_all, build_end_to_end


@pytest.fixture(scope="session")
def all_fixtures_dir(tmp_path_factory) -> Path:
    """Every fixture PDF (including the standalone multi-account / overdrawn
    cases), generated once per session."""
    d = tmp_path_factory.mktemp("all_pdfs")
    build_all(d)
    return d


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    """Only the fixtures that belong in the default folder-consolidation run."""
    d = tmp_path_factory.mktemp("statement_pdfs")
    build_end_to_end(d)
    return d


def _parse_one(fixture_dir: Path, name: str):
    from statement_parsers import parse_pdf
    from statement_parsers.base import STATUS_OK

    result = parse_pdf(fixture_dir / name)
    assert result.status == STATUS_OK, result.detail
    return result


@pytest.fixture(scope="session")
def regions_stmt(fixture_dir):
    result = _parse_one(fixture_dir, "regions_checking_2022-01.pdf")
    assert len(result.statements) == 1
    return result.statements[0]


@pytest.fixture(scope="session")
def servisfirst_stmt(fixture_dir):
    result = _parse_one(fixture_dir, "servisfirst_checking_2022-09.pdf")
    assert len(result.statements) == 1
    return result.statements[0]


@pytest.fixture(scope="session")
def servisfirst_multi(all_fixtures_dir):
    return _parse_one(all_fixtures_dir, "servisfirst_multi_account.pdf")


@pytest.fixture(scope="session")
def servisfirst_overdrawn(all_fixtures_dir):
    result = _parse_one(all_fixtures_dir, "servisfirst_overdrawn.pdf")
    assert len(result.statements) == 1
    return result.statements[0]
