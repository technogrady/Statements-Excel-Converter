"""Cross-statement consolidation: dedup, account grouping, balance
chaining, label variance, and missing-month detection.

Sits between the per-file parsers (``statement_parsers``) and the
workbook writer (``excel_writer``): parsers know one statement at a
time, this module reasons about the whole collection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from statement_parsers.base import FileResult, ParsedStatement, STATUS_OK, money_str

CHAIN_CONTINUOUS = "Continuous"
CHAIN_GAPS = "Gap(s)"
CHAIN_DISCONTINUITY = "⚠ Discontinuity"
CHAIN_SINGLE = "Single statement"


@dataclass
class LabelRun:
    """A consecutive run of statements carrying the same product label."""
    label: str
    first_start: date
    last_end: date

    def display(self) -> str:
        return f"{self.label} ({self.first_start:%Y-%m}–{self.last_end:%Y-%m})"


@dataclass
class AccountGroup:
    bank: str
    account_number_full: str | None
    last4: str
    statements: list[ParsedStatement]  # deduped, sorted by period_start
    suffix: str = ""  # "_a"/"_b" when a masked last4 splits into parallel chains
    label_runs: list[LabelRun] = field(default_factory=list)
    chain_status: str = CHAIN_CONTINUOUS
    chain_notes: list[str] = field(default_factory=list)
    missing_months: list[str] = field(default_factory=list)

    @property
    def display_account(self) -> str:
        return f"x{self.last4}{self.suffix}"

    @property
    def sheet_base_name(self) -> str:
        return f"{self.bank}_x{self.last4}{self.suffix}"

    @property
    def account_number_display(self) -> str:
        """Full number when the statements print one, else the masked form."""
        return self.account_number_full or f"x{self.last4}"


@dataclass
class Consolidation:
    file_results: list[FileResult]
    groups: list[AccountGroup]
    # statement kept out of the workbook because an identical-key statement
    # came first: (duplicate statement, source_file of the one kept)
    duplicates: list[tuple[ParsedStatement, str]]


def consolidate(file_results: list[FileResult]) -> Consolidation:
    unique, duplicates = _dedup_statements(file_results)
    groups = _group_accounts(unique)
    groups = _split_parallel_chains(groups)
    for g in groups:
        _analyze_group(g)
    groups.sort(key=lambda g: (g.bank, g.last4, g.suffix))
    return Consolidation(file_results=file_results, groups=groups, duplicates=duplicates)


# ---------------------------------------------------------------------------
# Deduplication — people re-download statements.
# ---------------------------------------------------------------------------

def _dedup_statements(
    file_results: list[FileResult],
) -> tuple[list[ParsedStatement], list[tuple[ParsedStatement, str]]]:
    """Statement-level dedup: key (bank, last4, period_start, period_end).
    First file (processing order) wins; later ones are excluded and reported.

    Exception: statements printing *different full account numbers* are
    definitively different accounts even when last4 and period collide, so
    they are never treated as duplicates of each other.

    Individual transactions are never deduped across different statements —
    identical-looking transactions legitimately repeat."""
    seen: dict[tuple, list[ParsedStatement]] = {}
    unique: list[ParsedStatement] = []
    duplicates: list[tuple[ParsedStatement, str]] = []
    for fr in file_results:
        if fr.status != STATUS_OK:
            continue
        for stmt in fr.statements:
            key = (stmt.bank, stmt.account_last4, stmt.period_start, stmt.period_end)
            kept = seen.setdefault(key, [])
            original = next(
                (
                    k for k in kept
                    if k.account_number_full is None
                    or stmt.account_number_full is None
                    or k.account_number_full == stmt.account_number_full
                ),
                None,
            )
            if original is not None:
                duplicates.append((stmt, original.source_file))
            else:
                kept.append(stmt)
                unique.append(stmt)
    return unique, duplicates


# ---------------------------------------------------------------------------
# Account grouping — full numbers are authoritative; masked fall back to last4.
# ---------------------------------------------------------------------------

def _group_accounts(statements: list[ParsedStatement]) -> list[AccountGroup]:
    full_groups: dict[tuple[str, str], AccountGroup] = {}
    masked_groups: dict[tuple[str, str], AccountGroup] = {}
    for stmt in statements:
        if stmt.account_number_full:
            key = (stmt.bank, stmt.account_number_full)
            g = full_groups.setdefault(
                key,
                AccountGroup(stmt.bank, stmt.account_number_full, stmt.account_last4, []),
            )
        else:
            key = (stmt.bank, stmt.account_last4)
            g = masked_groups.setdefault(
                key, AccountGroup(stmt.bank, None, stmt.account_last4, [])
            )
        g.statements.append(stmt)

    # A masked group may be the same account as a full-number group whose
    # number ends in the same last4. Merge ONLY when the merge is unambiguous
    # (exactly one candidate) AND balance chaining links the two sets.
    groups = list(full_groups.values())
    for (bank, last4), masked in masked_groups.items():
        candidates = [
            g for g in full_groups.values()
            if g.bank == bank and g.last4 == last4
        ]
        if len(candidates) == 1 and _chains_across(candidates[0], masked):
            target = candidates[0]
            target.statements.extend(masked.statements)
            target.chain_notes.append(
                f"Merged {len(masked.statements)} statement(s) with masked account "
                f"number into full-number account ····{last4} "
                "(last4 match + balance chain links)"
            )
        else:
            if len(candidates) > 1:
                masked.chain_notes.append(
                    f"{len(candidates)} full-number accounts share last4 {last4} — "
                    "masked statements kept as a separate group"
                )
            groups.append(masked)
    return groups


def _sorted_stmts(stmts: list[ParsedStatement]) -> list[ParsedStatement]:
    return sorted(stmts, key=lambda s: (s.period_start, s.period_end, s.source_file))


def _adjacent(prev: ParsedStatement, nxt: ParsedStatement) -> bool:
    """Periods are adjacent or overlap-by-one-day (normal cycle behavior)."""
    return nxt.period_start <= prev.period_end + timedelta(days=1)


def _chains_across(a: AccountGroup, b: AccountGroup) -> bool:
    """True if merging the two groups introduces no balance break at any
    boundary where a statement from one group meets a statement from the other."""
    tagged = [(s, 0) for s in a.statements] + [(s, 1) for s in b.statements]
    tagged.sort(key=lambda t: (t[0].period_start, t[0].period_end, t[0].source_file))
    crossings = 0
    for (prev, ptag), (nxt, ntag) in zip(tagged, tagged[1:]):
        if ptag != ntag:
            crossings += 1
            if prev.closing_balance != nxt.opening_balance:
                return False
    return crossings > 0


# ---------------------------------------------------------------------------
# Parallel-chain splitting — two accounts can share a masked last4.
# ---------------------------------------------------------------------------

def _split_parallel_chains(groups: list[AccountGroup]) -> list[AccountGroup]:
    out: list[AccountGroup] = []
    for g in groups:
        g.statements = _sorted_stmts(g.statements)
        if g.account_number_full is not None or len(g.statements) < 2:
            out.append(g)
            continue
        breaks = [
            (prev, nxt)
            for prev, nxt in zip(g.statements, g.statements[1:])
            if _adjacent(prev, nxt) and prev.closing_balance != nxt.opening_balance
        ]
        if not breaks:
            out.append(g)
            continue
        chains = _partition_chains(g.statements)
        if len(chains) == 2 and all(len(c) >= 1 for c in chains):
            for chain, suffix in zip(chains, ("_a", "_b")):
                files = ", ".join(s.source_file for s in chain)
                sub = AccountGroup(
                    bank=g.bank,
                    account_number_full=None,
                    last4=g.last4,
                    statements=chain,
                    suffix=suffix,
                    chain_notes=list(g.chain_notes) + [
                        f"⚠ Possible second account under x{g.last4}: statements split "
                        f"into self-consistent chains; this chain ({suffix[1:]}) = {files} "
                        "— verify manually"
                    ],
                )
                out.append(sub)
        else:
            out.append(g)  # discontinuities get reported by _analyze_group
    return out


def _partition_chains(stmts: list[ParsedStatement]) -> list[list[ParsedStatement]]:
    """Greedy partition into balance-linked chains: each statement attaches to
    the chain whose tail closing balance equals its opening balance (and whose
    tail period precedes it); otherwise it starts a new chain."""
    chains: list[list[ParsedStatement]] = []
    for stmt in stmts:
        best = None
        for chain in chains:
            tail = chain[-1]
            if (
                tail.closing_balance == stmt.opening_balance
                and tail.period_end <= stmt.period_end
                and (best is None or chain[-1].period_end > best[-1].period_end)
            ):
                best = chain
        if best is not None:
            best.append(stmt)
        else:
            chains.append([stmt])
    return chains


# ---------------------------------------------------------------------------
# Per-group analysis: chain status, label runs, missing months.
# ---------------------------------------------------------------------------

def _analyze_group(g: AccountGroup) -> None:
    g.statements = _sorted_stmts(g.statements)
    stmts = g.statements

    # Label runs (product renames over the years are informational only).
    for stmt in stmts:
        if g.label_runs and g.label_runs[-1].label == stmt.account_label:
            g.label_runs[-1].last_end = max(g.label_runs[-1].last_end, stmt.period_end)
        else:
            g.label_runs.append(
                LabelRun(stmt.account_label, stmt.period_start, stmt.period_end)
            )

    if len(stmts) == 1:
        g.chain_status = CHAIN_SINGLE
        g.missing_months = []
        return

    has_gap = False
    has_discontinuity = False
    for prev, nxt in zip(stmts, stmts[1:]):
        linked = prev.closing_balance == nxt.opening_balance
        adjacent = _adjacent(prev, nxt)
        if linked:
            if not adjacent:
                has_gap = True
                g.chain_notes.append(
                    f"Gap in coverage between {prev.period_end.isoformat()} and "
                    f"{nxt.period_start.isoformat()} (balances still link — "
                    "possibly a no-activity period with missing statements)"
                )
            elif prev.account_label != nxt.account_label:
                g.chain_notes.append(
                    f"Label changed (same account): '{prev.account_label}' → "
                    f"'{nxt.account_label}' — balance chain links across the change"
                )
        elif not adjacent:
            has_gap = True
            g.chain_notes.append(
                f"Gap: missing statement(s) between {prev.period_end.isoformat()} and "
                f"{nxt.period_start.isoformat()} "
                f"(closing {money_str(prev.closing_balance)} ≠ next opening "
                f"{money_str(nxt.opening_balance)})"
            )
        else:
            has_discontinuity = True
            extra = (
                " — account identity is certain (full account number); indicates a "
                "missing statement or parsing error"
                if g.account_number_full
                else ""
            )
            g.chain_notes.append(
                f"⚠ Balance discontinuity: {prev.source_file} closes at "
                f"{money_str(prev.closing_balance)} on {prev.period_end.isoformat()} but "
                f"{nxt.source_file} opens at {money_str(nxt.opening_balance)}{extra}"
            )

    if has_discontinuity:
        g.chain_status = CHAIN_DISCONTINUITY
    elif has_gap:
        g.chain_status = CHAIN_GAPS
    else:
        g.chain_status = CHAIN_CONTINUOUS

    g.missing_months = _missing_months(stmts)


def _missing_months(stmts: list[ParsedStatement]) -> list[str]:
    """Months between the earliest and latest covered period not touched by
    any statement period."""
    earliest = min(s.period_start for s in stmts)
    latest = max(s.period_end for s in stmts)
    covered: set[tuple[int, int]] = set()
    for s in stmts:
        y, m = s.period_start.year, s.period_start.month
        while (y, m) <= (s.period_end.year, s.period_end.month):
            covered.add((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    missing = []
    y, m = earliest.year, earliest.month
    while (y, m) <= (latest.year, latest.month):
        if (y, m) not in covered:
            missing.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return missing


# ---------------------------------------------------------------------------
# Convenience aggregates used by the writer and the run summary.
# ---------------------------------------------------------------------------

def total_credits(stmt: ParsedStatement) -> Decimal:
    return sum((t.amount for t in stmt.transactions if t.amount > 0), Decimal("0"))


def total_debits(stmt: ParsedStatement) -> Decimal:
    return sum((t.amount for t in stmt.transactions if t.amount < 0), Decimal("0"))
