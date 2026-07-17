from datetime import date
from decimal import Decimal

from consolidation import (
    CHAIN_CONTINUOUS,
    CHAIN_DISCONTINUITY,
    CHAIN_GAPS,
    CHAIN_SINGLE,
    consolidate,
)
from statement_parsers.base import STATUS_OK, FileResult, ParsedStatement


def mk_stmt(
    *,
    bank="Regions",
    full=None,
    last4="1000",
    label="BUSINESS CHECKING",
    start,
    end,
    opening,
    closing,
    source="a.pdf",
):
    return ParsedStatement(
        bank=bank,
        account_number_full=full,
        account_last4=last4,
        account_label=label,
        period_start=start,
        period_end=end,
        opening_balance=Decimal(opening),
        closing_balance=Decimal(closing),
        transactions=[],
        source_file=source,
    )


def wrap(*stmts):
    return [FileResult(s.source_file, STATUS_OK, statements=[s]) for s in stmts]


class TestDedup:
    def test_same_key_second_file_marked_duplicate(self):
        a = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="first.pdf")
        b = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="second.pdf")
        c = consolidate(wrap(a, b))
        assert len(c.duplicates) == 1
        dup, kept_source = c.duplicates[0]
        assert dup.source_file == "second.pdf"
        assert kept_source == "first.pdf"
        assert sum(len(g.statements) for g in c.groups) == 1

    def test_different_periods_not_duplicates(self):
        a = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="200", closing="300", source="feb.pdf")
        c = consolidate(wrap(a, b))
        assert c.duplicates == []
        assert len(c.groups[0].statements) == 2


class TestBalanceChain:
    def test_continuous(self):
        a = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="200", closing="300", source="feb.pdf")
        g = consolidate(wrap(a, b)).groups[0]
        assert g.chain_status == CHAIN_CONTINUOUS
        assert g.missing_months == []

    def test_single_statement(self):
        a = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200")
        assert consolidate(wrap(a)).groups[0].chain_status == CHAIN_SINGLE

    def test_gap_with_missing_months(self):
        a = mk_stmt(start=date(2021, 1, 1), end=date(2021, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(start=date(2021, 5, 1), end=date(2021, 5, 31),
                    opening="500", closing="600", source="may.pdf")
        g = consolidate(wrap(a, b)).groups[0]
        assert g.chain_status == CHAIN_GAPS
        assert g.missing_months == ["2021-02", "2021-03", "2021-04"]
        assert any("Gap: missing statement" in n for n in g.chain_notes)

    def test_adjacent_but_unlinked_is_discontinuity(self):
        a = mk_stmt(full="0011221000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(full="0011221000", start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="999", closing="1200", source="feb.pdf")
        g = consolidate(wrap(a, b)).groups[0]
        assert g.chain_status == CHAIN_DISCONTINUITY
        # full-number group → identity is certain, message says so
        assert any("account identity is certain" in n for n in g.chain_notes)

    def test_overlap_by_one_day_is_adjacent(self):
        a = mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(start=date(2020, 1, 31), end=date(2020, 2, 28),
                    opening="200", closing="250", source="feb.pdf")
        assert consolidate(wrap(a, b)).groups[0].chain_status == CHAIN_CONTINUOUS


class TestLabelVariance:
    def test_label_change_with_linking_chain_is_informational(self):
        a = mk_stmt(label="BUSINESS INTEREST CHECKING",
                    start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf")
        b = mk_stmt(label="BUSINESS CHECKINGS",
                    start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="200", closing="300", source="feb.pdf")
        g = consolidate(wrap(a, b)).groups[0]
        assert g.chain_status == CHAIN_CONTINUOUS
        assert any("Label changed (same account)" in n for n in g.chain_notes)
        assert [r.label for r in g.label_runs] == [
            "BUSINESS INTEREST CHECKING", "BUSINESS CHECKINGS",
        ]


class TestAccountGrouping:
    def test_different_full_numbers_same_last4_stay_separate(self):
        a = mk_stmt(full="0011111000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="a.pdf")
        b = mk_stmt(full="0022221000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="500", closing="600", source="b.pdf")
        c = consolidate(wrap(a, b))
        assert len(c.groups) == 2  # NOT merged and NOT deduped despite same last4+period
        assert c.duplicates == []  # different full numbers are different accounts

    def test_masked_merges_into_full_when_chain_links(self):
        a = mk_stmt(full="0011221000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="full.pdf")
        b = mk_stmt(full=None, start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="200", closing="300", source="masked.pdf")
        c = consolidate(wrap(a, b))
        assert len(c.groups) == 1
        assert c.groups[0].account_number_full == "0011221000"
        assert len(c.groups[0].statements) == 2

    def test_masked_kept_separate_when_chain_does_not_link(self):
        a = mk_stmt(full="0011221000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="full.pdf")
        b = mk_stmt(full=None, start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="9999", closing="9000", source="masked.pdf")
        c = consolidate(wrap(a, b))
        assert len(c.groups) == 2


class TestParallelChainSplit:
    def test_two_interleaved_accounts_under_same_masked_last4(self):
        # chain A: 100→200→300 ; chain B: 5000→5500→6000, same last4, same periods
        stmts = [
            mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="a-jan.pdf"),
            mk_stmt(start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="5000", closing="5500", source="b-jan.pdf"),
            mk_stmt(start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="200", closing="300", source="a-feb.pdf"),
            mk_stmt(start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="5500", closing="6000", source="b-feb.pdf"),
        ]
        # different periods share the (bank, last4, period) dedup key only when
        # periods match — these pairs have identical periods, so tweak keys:
        stmts[1].period_end = date(2020, 1, 30)
        stmts[3].period_end = date(2020, 2, 28)
        c = consolidate(wrap(*stmts))
        assert len(c.groups) == 2
        suffixes = sorted(g.suffix for g in c.groups)
        assert suffixes == ["_a", "_b"]
        for g in c.groups:
            assert any("Possible second account" in n for n in g.chain_notes)
        # _a/_b assignment order is arbitrary; the partition itself must be exact
        partition = {frozenset(s.source_file for s in g.statements) for g in c.groups}
        assert partition == {
            frozenset({"a-jan.pdf", "a-feb.pdf"}),
            frozenset({"b-jan.pdf", "b-feb.pdf"}),
        }
        # and each split group must list its member files for manual verification
        for g in c.groups:
            note = next(n for n in g.chain_notes if "Possible second account" in n)
            for s in g.statements:
                assert s.source_file in note

    def test_full_number_group_never_splits(self):
        stmts = [
            mk_stmt(full="0011221000", start=date(2020, 1, 1), end=date(2020, 1, 31),
                    opening="100", closing="200", source="jan.pdf"),
            mk_stmt(full="0011221000", start=date(2020, 2, 1), end=date(2020, 2, 29),
                    opening="999", closing="1100", source="feb.pdf"),
        ]
        c = consolidate(wrap(*stmts))
        assert len(c.groups) == 1
        assert c.groups[0].chain_status == CHAIN_DISCONTINUITY
