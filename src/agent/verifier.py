"""
Deterministic verification of a commentary draft. No LLM is involved.

Every number is checked twice:

  Check 1 — prose vs. fact sheet. Each quantity written in a sentence
  ($ amounts, percentages, percentage points, basis points) is parsed and
  must be the correct rounding of a cited fact, or of a difference / percent
  change between cited facts. Direction words ("up", "fell") must agree with
  the sign. Unknown fact IDs, uncited numbers and stray numerals fail.

  Check 2 — fact sheet vs. filings. Every cited fact, and every fact it was
  computed from, is re-derived independently: reported values are looked up
  in the raw SEC filing rows (bronze `num`, keyed by accession number + tag +
  period) through a SourceLedger; pipeline-derived values (Q4, gross profit,
  margins, changes) are recomputed from their inputs.

Policy checks run too: non-GAAP measures and forward-looking statements are
errors; causal language is a warning the reviewer must confirm, because the
filings' numbers can't support a claim about *why* something moved.

A draft with any error is blocked from approval; warnings are surfaced to the
human reviewer.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Protocol

from src.agent.drafter import Draft
from src.agent.facts import DIFFERENCE, PCT_CHANGE, RATIO, RATIO_FORMULA, USD, Fact, FactSheet

ERROR, WARNING, INFO = "error", "warning", "info"
SOURCE_TOLERANCE_USD = 1.0  # SEC values are whole dollars
RECOMPUTE_TOLERANCE = 1e-9


@dataclass
class Issue:
    level: str
    code: str
    message: str
    sentence_index: int | None = None
    fact_id: str | None = None


@dataclass
class VerificationReport:
    issues: list[Issue] = field(default_factory=list)
    numbers_checked: int = 0
    facts_source_checked: int = 0

    @property
    def passed(self) -> bool:
        return not any(i.level == ERROR for i in self.issues)

    def add(self, *args, **kwargs) -> None:
        self.issues.append(Issue(*args, **kwargs))

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "numbers_checked": self.numbers_checked,
            "facts_source_checked": self.facts_source_checked,
            "issues": [asdict(i) for i in self.issues],
        }

    def summary(self) -> str:
        n_err = sum(i.level == ERROR for i in self.issues)
        n_warn = sum(i.level == WARNING for i in self.issues)
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"{status}: {self.numbers_checked} numbers checked against cited facts, "
            f"{self.facts_source_checked} facts re-derived from filings; {n_err} errors, {n_warn} warnings"
        )


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------


@dataclass
class Quantity:
    kind: str  # money | percent | points (a fraction; bps and pp both normalize here)
    value: float  # absolute magnitude, in dollars / as a fraction
    tolerance: float  # half a unit of the last written digit, same units as value
    text: str
    negative: bool = False


_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_SCALES = {
    "trillion": 1e12,
    "billion": 1e9,
    "million": 1e6,
    "thousand": 1e3,
    "t": 1e12,
    "b": 1e9,
    "bn": 1e9,
    "m": 1e6,
    "mm": 1e6,
    "k": 1e3,
}
MONEY_RE = re.compile(r"(-|−)?\$\s?" + _NUM + r"(?:\s?(trillion|billion|million|thousand|bn|mm|[tbmk])\b)?", re.I)
POINTS_RE = re.compile(_NUM + r"\s?(percentage points?|pp|ppts?|bps|basis points?)\b", re.I)
PERCENT_RE = re.compile(r"(-|−)?" + _NUM + r"\s?(%|percent\b)", re.I)
# Tokens that may legitimately contain digits: period labels, years, form names.
PERIOD_RE = re.compile(
    r"\b(?:FY\s?)?\d{4}\s?Q[1-4]\b|\bQ[1-4](?:\s?(?:FY)?\s?\d{2,4})?\b|\b(?:19|20)\d{2}\b|\b10-[KQ](?:/A)?\b", re.I
)
# Ordinals name a position, not a quantity: "the 25th percentile" is a label
# for a value that is itself cited and checked. "3 straight quarters" has no
# ordinal suffix and stays flagged.
ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I)
BARE_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

UP_WORDS = re.compile(
    r"\b(up|rose|rise|rising|increas\w*|grew|grow\w*|higher|expan\w*|improv\w*|gain\w*|climb\w*)\b", re.I
)
DOWN_WORDS = re.compile(
    r"\b(down|fell|fall\w*|declin\w*|decreas\w*|lower|contract\w*|compress\w*|drop\w*|shr[aui]nk\w*)\b", re.I
)


def _half_unit(number_text: str) -> float:
    decimals = len(number_text.split(".")[1]) if "." in number_text else 0
    return 0.5 * 10 ** (-decimals)


def extract_quantities(text: str, exempt: tuple[str, ...] = ()) -> tuple[list[Quantity], list[str]]:
    """Returns (quantities, stray_numerals). Stray numerals are digits that
    aren't a recognizable financial quantity, year, or quarter label.

    `exempt` holds literal phrases whose digits are names, not quantities —
    the company's own name above all. Plenty of filers have a digit in the
    name (3M, 7-Eleven, 1-800-Flowers), and flagging the subject of the
    sentence as an unverifiable figure would be a false positive on every
    sentence that names it."""
    quantities: list[Quantity] = []
    spans: list[tuple[int, int]] = []

    for m in MONEY_RE.finditer(text):
        num = m.group(2)
        scale = _SCALES[m.group(3).lower()] if m.group(3) else 1.0
        quantities.append(
            Quantity(
                "money",
                float(num.replace(",", "")) * scale,
                _half_unit(num) * scale,
                m.group(0),
                negative=bool(m.group(1)),
            )
        )
        spans.append(m.span())
    for m in POINTS_RE.finditer(text):
        num, unit = m.group(1), m.group(2).lower()
        per = 1e-4 if unit.startswith(("bp", "basis")) else 1e-2
        quantities.append(Quantity("points", float(num.replace(",", "")) * per, _half_unit(num) * per, m.group(0)))
        spans.append(m.span())
    for m in PERCENT_RE.finditer(text):
        if any(s <= m.start(2) < e for s, e in spans):
            continue
        num = m.group(2)
        quantities.append(
            Quantity(
                "percent",
                float(num.replace(",", "")) / 100,
                _half_unit(num) / 100,
                m.group(0),
                negative=bool(m.group(1)),
            )
        )
        spans.append(m.span())

    masked = list(text)
    for s, e in spans:
        masked[s:e] = " " * (e - s)
    remainder = "".join(masked)
    # Longest first, so "3M Company" is removed before a bare "3M".
    for phrase in sorted((p for p in exempt if p and any(ch.isdigit() for ch in p)), key=len, reverse=True):
        remainder = re.sub(re.escape(phrase), " ", remainder, flags=re.I)
    remainder = ORDINAL_RE.sub(" ", PERIOD_RE.sub(" ", remainder))
    strays = BARE_NUMBER_RE.findall(remainder)
    return quantities, strays


# ---------------------------------------------------------------------------
# Check 1: prose vs. cited facts
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    kind: str
    value: float  # signed
    is_change: bool
    description: str


def _candidates(cited: list[Fact]) -> list[Candidate]:
    out: list[Candidate] = []
    usd = [f for f in cited if f.unit == USD]
    ratios = [f for f in cited if f.unit == RATIO]
    for f in usd:
        out.append(Candidate("money", f.value, False, f.id))
    for f in ratios:
        is_change = f.formula in (PCT_CHANGE, DIFFERENCE) or f.metric.endswith(("_pct_change", "_qoq_pct", "_yoy_pct"))
        kind = "points" if f.formula == DIFFERENCE else "percent"
        out.append(Candidate(kind, f.value, is_change, f.id))
    for a in usd:
        for b in usd:
            if a is b:
                continue
            if a.metric == b.metric and b.value != 0:
                out.append(Candidate("money", a.value - b.value, True, f"{a.id} - {b.id}"))
                out.append(
                    Candidate("percent", (a.value - b.value) / abs(b.value), True, f"pct_change({a.id}, {b.id})")
                )
            elif a.period == b.period and b.metric == "revenue" and b.value != 0:
                out.append(Candidate("percent", a.value / b.value, False, f"{a.id} / {b.id}"))
    for a in ratios:
        for b in ratios:
            if a is not b and a.metric == b.metric and a.formula != DIFFERENCE:
                out.append(Candidate("points", a.value - b.value, True, f"{a.id} - {b.id}"))
    return out


def _matches(q: Quantity, c: Candidate) -> bool:
    return q.kind == c.kind and abs(abs(c.value) - q.value) <= q.tolerance * (1 + 1e-9) + 1e-12


def _exempt_phrases(sheet: FactSheet) -> tuple[str, ...]:
    """Names that may legitimately contain digits: the company's own name and
    every entity on the fact sheet (peers are named in the prose too)."""
    names = {sheet.company_name, sheet.ticker} | {f.entity for f in sheet.facts.values()}
    return tuple(n for n in names if n)


def check_sentence(
    idx: int,
    text: str,
    fact_ids: list[str],
    sheet: FactSheet,
    report: VerificationReport,
    exempt: tuple[str, ...] = (),
) -> None:
    cited = []
    for fid in fact_ids:
        fact = sheet.get(fid)
        if fact is None:
            report.add(ERROR, "unknown_fact_id", f"Cited fact '{fid}' is not on the fact sheet.", idx, fid)
        else:
            cited.append(fact)

    quantities, strays = extract_quantities(text, exempt)
    for s in strays:
        report.add(
            ERROR,
            "unverifiable_numeral",
            f"Numeral '{s}' is not a recognizable amount, percentage, year or quarter label.",
            idx,
        )
    if quantities and not cited:
        report.add(ERROR, "uncited_number", "Sentence contains numbers but cites no facts.", idx)
        return

    candidates = _candidates(cited)
    says_up, says_down = bool(UP_WORDS.search(text)), bool(DOWN_WORDS.search(text))
    for q in quantities:
        report.numbers_checked += 1
        matches = [c for c in candidates if _matches(q, c)]
        if not matches:
            closest = min(
                (c for c in candidates if c.kind == q.kind), key=lambda c: abs(abs(c.value) - q.value), default=None
            )
            hint = f" Closest cited value: {closest.description} = {closest.value:.6g}." if closest else ""
            report.add(
                ERROR,
                "number_mismatch",
                f"'{q.text}' does not match any cited fact at the precision written.{hint}",
                idx,
            )
            continue
        if q.negative:
            if not any(c.value < 0 for c in matches):
                report.add(
                    ERROR, "sign_mismatch", f"'{q.text}' is written as negative but the cited value is positive.", idx
                )
            continue
        levels = [c for c in matches if not c.is_change]
        changes = [c for c in matches if c.is_change]
        if levels or not changes or says_up == says_down:
            continue  # a level, or no unambiguous direction word to check
        if says_up and not any(c.value >= 0 for c in changes):
            report.add(ERROR, "direction_mismatch", f"Sentence says the value rose, but '{q.text}' is a decrease.", idx)
        if says_down and not any(c.value <= 0 for c in changes):
            report.add(
                ERROR, "direction_mismatch", f"Sentence says the value fell, but '{q.text}' is an increase.", idx
            )


# ---------------------------------------------------------------------------
# Policy checks
# ---------------------------------------------------------------------------

POLICY_RULES = [
    (
        ERROR,
        "non_gaap",
        re.compile(r"\bnon-?gaap\b|\badjusted (?:ebitda|eps|earnings|operating|gross|net)\b|\bebitda\b", re.I),
        "Non-GAAP measures are not in XBRL and are out of scope.",
    ),
    (
        ERROR,
        "forward_looking",
        re.compile(
            r"\b(guidance|outlook|forecast\w*|expects?|expected to|anticipat\w*"
            r"|will (?:likely|continue|be|see|grow|decline))\b",
            re.I,
        ),
        "Forward-looking statements are out of scope for historical variance commentary.",
    ),
    (
        WARNING,
        "causal_claim",
        re.compile(
            r"\b(due to|driven by|because of|as a result of|owing to|thanks to"
            r"|reflect(?:s|ed|ing)? (?:strong|weak|higher|lower))\b",
            re.I,
        ),
        "Causal language: filings don't state drivers. Reviewer must confirm the explanation with the business.",
    ),
]


def check_policy(idx: int, text: str, report: VerificationReport) -> None:
    for level, code, pattern, message in POLICY_RULES:
        m = pattern.search(text)
        if m:
            report.add(level, code, f"{message} (matched '{m.group(0)}')", idx)


# ---------------------------------------------------------------------------
# Check 2: fact sheet vs. filings
# ---------------------------------------------------------------------------


class SourceLedger(Protocol):
    def values(self, adsh: str, tag: str, qtrs: int) -> dict[str, float]:
        """{period end date ISO string: value} for one tag in one filing,
        consolidated USD facts only, exactly as filed."""
        ...


class InMemoryLedger:
    def __init__(self, rows: list[tuple[str, str, str, int, float]]):
        """rows: (adsh, tag, ddate ISO, qtrs, value)"""
        self._rows: dict[tuple[str, str, int], dict[str, float]] = {}
        for adsh, tag, ddate, qtrs, value in rows:
            self._rows.setdefault((adsh, tag, int(qtrs)), {})[ddate] = float(value)

    def values(self, adsh: str, tag: str, qtrs: int) -> dict[str, float]:
        return self._rows.get((adsh, tag, int(qtrs)), {})

    def rows(self) -> list[tuple[str, str, str, int, float]]:
        return [
            (adsh, tag, ddate, qtrs, value)
            for (adsh, tag, qtrs), by_date in self._rows.items()
            for ddate, value in by_date.items()
        ]

    def with_override(self, adsh: str, tag: str, ddate: str, qtrs: int, value: float) -> InMemoryLedger:
        """A copy with one filed value changed — used by the eval harness to
        simulate a pipeline bug between the raw filing and the fact sheet."""
        kept = [r for r in self.rows() if r[:4] != (adsh, tag, ddate, int(qtrs))]
        return InMemoryLedger([*kept, (adsh, tag, ddate, int(qtrs), value)])


def _close(a: float, b: float, unit: str) -> bool:
    if unit == USD:
        return abs(a - b) <= SOURCE_TOLERANCE_USD
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=RECOMPUTE_TOLERANCE)


def _recompute_formula(fact: Fact, sheet: FactSheet) -> float | None:
    inputs = [sheet.get(i) for i in fact.inputs]
    if any(i is None for i in inputs) or len(inputs) != 2:
        return None
    a, b = inputs[0].value, inputs[1].value
    if fact.formula == RATIO_FORMULA:
        return a / b if b else None
    if fact.formula == PCT_CHANGE:
        return (a - b) / abs(b) if b else None
    if fact.formula == DIFFERENCE:
        return a - b
    return None


def _verify_reported(fact: Fact, ledger: SourceLedger) -> tuple[bool, str]:
    src = fact.source
    filed = ledger.values(src.adsh, src.tag, src.qtrs)
    if src.ddate not in filed:
        return False, f"no {src.tag} (qtrs={src.qtrs}, period ending {src.ddate}) found in filing {src.adsh}"
    if not _close(filed[src.ddate], fact.value, USD):
        return False, f"filing {src.adsh} reports {filed[src.ddate]:.0f}, fact sheet has {fact.value:.0f}"
    return True, ""


def _verify_derived_q4(fact: Fact, ledger: SourceLedger) -> tuple[bool, str]:
    src = fact.source
    annual_adsh, ytd_adsh = src.adsh.split("|")
    annual_tag, ytd_tag = src.tag.split("|")
    annual = ledger.values(annual_adsh, annual_tag, 4).get(src.ddate)
    end = date.fromisoformat(src.ddate)
    window = {(end - timedelta(days=d)).isoformat() for d in range(80, 101)}
    ytd = [v for d, v in ledger.values(ytd_adsh, ytd_tag, 3).items() if d in window]
    if annual is None or len(ytd) != 1:
        return False, f"could not find annual ({annual_adsh}) and nine-month ({ytd_adsh}) values to re-derive Q4"
    if not _close(annual - ytd[0], fact.value, USD):
        return (
            False,
            f"annual {annual:.0f} - nine-month {ytd[0]:.0f} = {annual - ytd[0]:.0f}, fact sheet has {fact.value:.0f}",
        )
    return True, ""


def check_sources(
    cited_ids: set[str], sheet: FactSheet, ledger: SourceLedger | None, report: VerificationReport
) -> None:
    to_visit, seen = list(cited_ids), set()
    while to_visit:
        fid = to_visit.pop()
        if fid in seen:
            continue
        seen.add(fid)
        fact = sheet.get(fid)
        if fact is None:
            continue

        if fact.formula:
            to_visit.extend(fact.inputs)
            recomputed = _recompute_formula(fact, sheet)
            if recomputed is None or not _close(recomputed, fact.value, fact.unit):
                report.add(
                    ERROR, "recompute_mismatch", f"{fid} does not equal {fact.formula} of its inputs.", fact_id=fid
                )
            continue

        if fact.source is None:
            report.add(
                INFO,
                "aggregate_not_traced",
                f"{fid} is a peer/sector aggregate from the gold layer, not traced to a single filing.",
                fact_id=fid,
            )
            continue

        if fact.source.derivation == "revenue_minus_cost_of_revenue":
            rev = sheet.get(f"{fact.entity}.revenue.{fact.period}")
            cost = sheet.get(f"{fact.entity}.cost_of_revenue.{fact.period}")
            if rev is None or cost is None or not _close(rev.value - cost.value, fact.value, USD):
                report.add(ERROR, "recompute_mismatch", f"{fid} does not equal revenue - cost of revenue.", fact_id=fid)
            else:
                to_visit.extend([rev.id, cost.id])
            continue

        if ledger is None:
            report.add(WARNING, "source_not_checked", f"{fid}: no filing ledger available.", fact_id=fid)
            continue
        ok, why = (_verify_derived_q4 if fact.source.derivation == "annual_minus_ytd9" else _verify_reported)(
            fact, ledger
        )
        report.facts_source_checked += 1
        if not ok:
            report.add(ERROR, "source_mismatch", f"{fid}: {why}", fact_id=fid)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def verify(draft: Draft, sheet: FactSheet, ledger: SourceLedger | None) -> VerificationReport:
    report = VerificationReport()
    if not draft.sentences:
        report.add(ERROR, "empty_draft", "Draft contains no sentences.")
    exempt = _exempt_phrases(sheet)
    cited: set[str] = set()
    for idx, s in enumerate(draft.sentences):
        check_sentence(idx, s.text, s.fact_ids, sheet, report, exempt)
        check_policy(idx, s.text, report)
        cited.update(fid for fid in s.fact_ids if sheet.get(fid))
    for q in draft.open_questions:
        quantities, strays = extract_quantities(q, exempt)
        if quantities or strays:
            report.add(ERROR, "number_in_open_question", f"Open questions must not contain numbers: '{q}'")
    check_sources(cited, sheet, ledger, report)
    return report


def spark_ledger(spark, sheet: FactSheet, bronze_dir) -> InMemoryLedger:
    """Loads, from bronze `num` (the SEC's rows exactly as published), every
    fact in the accession numbers the fact sheet cites."""
    from pyspark.sql import functions as F

    accessions = sorted({a for f in sheet.facts.values() if f.source for a in f.source.adsh.split("|") if a})
    num = spark.read.parquet(str(bronze_dir / "num")).filter(
        F.col("adsh").isin(accessions) & F.col("segments").isNull() & F.col("coreg").isNull() & (F.col("uom") == "USD")
    )
    rows = []
    for r in num.select("adsh", "tag", "ddate", "qtrs", "value").collect():
        if r["value"] is None:
            continue
        d = r["ddate"]
        rows.append((r["adsh"], r["tag"], f"{d[:4]}-{d[4:6]}-{d[6:8]}", int(r["qtrs"]), float(r["value"])))
    return InMemoryLedger(rows)
