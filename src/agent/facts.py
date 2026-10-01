"""
The fact sheet: the ONLY numbers the commentary agent is allowed to use.

Every fact has a stable, human-readable ID (e.g. `INTC.revenue.2024Q2`) that
the drafter must cite, and carries its lineage:

  - reported facts point at the exact filing (accession number), XBRL tag,
    period end date and duration they came from;
  - derived facts (margins, QoQ/YoY changes, basis-point moves) record the
    formula and the IDs of the facts they were computed from, so the verifier
    can recompute them rather than trust them.

This module is pure Python (dataclasses + JSON) so the agent, verifier and
review queue can be tested without Spark. `build_fact_sheet` is the one
function that reads the Spark gold/silver tables to populate it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Income-statement metrics the commentary covers, in reading order.
COMMENTARY_METRICS = [
    ("revenue", "Revenue"),
    ("cost_of_revenue", "Cost of revenue"),
    ("gross_profit", "Gross profit"),
    ("research_and_development_expense", "R&D expense"),
    ("sga_expense", "SG&A expense"),
    ("operating_income", "Operating income"),
    ("net_income", "Net income"),
]
RATIOS = [
    ("gross_margin_pct", "Gross margin", "gross_profit"),
    ("operating_margin_pct", "Operating margin", "operating_income"),
    ("net_margin_pct", "Net margin", "net_income"),
    ("rd_intensity_pct", "R&D as % of revenue", "research_and_development_expense"),
]
CHANGE_METRICS = ["revenue", "gross_profit", "operating_income", "net_income"]
PEER_RATIOS = ["gross_margin_pct", "operating_margin_pct"]

# Units
USD = "USD"
RATIO = "ratio"  # a fraction: 0.412 means 41.2%

# Formulas a derived fact can use. The verifier implements each one.
RATIO_FORMULA = "ratio"  # inputs [numerator, denominator]
PCT_CHANGE = "pct_change"  # inputs [current, prior]; (cur - prior) / |prior|
DIFFERENCE = "difference"  # inputs [a, b]; a - b (used for margin moves)


@dataclass(frozen=True)
class Source:
    """Where a reported number came from. `adsh` and `tag` hold '|'-joined
    lists when silver combined several filings (e.g. a derived Q4)."""

    adsh: str
    tag: str
    ddate: str
    qtrs: int
    form: str | None
    filed: str | None
    derivation: str
    is_derived: bool

    def filing_urls(self, cik: str) -> list[str]:
        return [edgar_filing_url(cik, a) for a in self.adsh.split("|") if a]


@dataclass(frozen=True)
class Fact:
    id: str
    entity: str  # ticker, or "SECTOR"
    cik: str | None
    metric: str
    label: str
    period: str  # calendar quarter label, e.g. "2024Q2"
    value: float
    unit: str  # USD or ratio
    source: Source | None = None  # reported facts
    formula: str | None = None  # derived facts
    inputs: tuple[str, ...] = ()  # derived facts: IDs of input facts
    note: str | None = None

    def display(self, precise: bool = False) -> str:
        return format_value(self.value, self.unit, self.formula, precise=precise)


@dataclass
class FactSheet:
    ticker: str
    cik: str
    company_name: str
    period: str
    prior_quarter: str
    year_ago_quarter: str
    facts: dict[str, Fact] = field(default_factory=dict)
    anomalies: list[dict] = field(default_factory=list)
    sector_sic_code: str | None = None

    def add(self, fact: Fact) -> None:
        self.facts[fact.id] = fact

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def to_prompt_table(self) -> str:
        """Compact, line-per-fact rendering for the LLM prompt."""
        lines = []
        for f in self.facts.values():
            shown = f.display(precise=True)
            extra = f" = {f.formula}({', '.join(f.inputs)})" if f.formula else ""
            flag = " [derived by pipeline]" if f.source and f.source.is_derived else ""
            lines.append(f"{f.id} | {f.label} | {f.period} | {shown}{extra}{flag}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> FactSheet:
        data = json.loads(raw)
        facts = {}
        for fid, f in data.pop("facts").items():
            raw_source = f.pop("source")
            source = Source(**raw_source) if raw_source else None
            facts[fid] = Fact(**{**f, "inputs": tuple(f["inputs"]), "source": source})
        return cls(**data, facts=facts)


def edgar_filing_url(cik: str, adsh: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{adsh}-index.htm"


def format_value(value: float, unit: str, formula: str | None = None, precise: bool = False) -> str:
    """`precise=True` is for the drafter's prompt; the default is for humans.

    The distinction matters more than it looks. A ratio shown to two decimals
    makes the drafter round twice: it is told to write one decimal, so it
    reads 14.65% off the fact sheet and writes 14.7%, when the true value is
    14.6474% and the right answer is 14.6%. The verifier then — correctly —
    rejects the figure. Two of the four failures in the first 20-draft eval
    were exactly this, and both were the pipeline's fault, not the model's.
    So the prompt gets enough precision to round once, and exact dollars
    rather than a pre-scaled amount."""
    if formula == DIFFERENCE:  # a margin move: percentage points, not percent
        return f"{value * 100:+.4f} pp" if precise else f"{value * 100:+.2f} pp"
    if unit == RATIO:
        return f"{value * 100:.4f}%" if precise else f"{value * 100:.2f}%"
    if precise:
        return f"${value:,.0f}"
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:,.3f}B"
    if magnitude >= 1e6:
        return f"{sign}${magnitude / 1e6:,.2f}M"
    return f"{sign}${magnitude:,.0f}"


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def parse_period(label: str) -> tuple[int, int]:
    """'2024Q2' -> (2024, 2). Accepts '2024q2' and '2024-Q2' too."""
    cleaned = label.upper().replace("-", "")
    year, quarter = cleaned.split("Q")
    q = int(quarter)
    if q not in (1, 2, 3, 4):
        raise ValueError(f"Bad quarter in {label!r}")
    return int(year), q


def period_label(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def shift_period(label: str, quarters: int) -> str:
    year, q = parse_period(label)
    idx = year * 4 + (q - 1) + quarters
    return period_label(idx // 4, idx % 4 + 1)


def fact_id(entity: str, metric: str, period: str) -> str:
    return f"{entity}.{metric}.{period}"


# ---------------------------------------------------------------------------
# Building a fact sheet from plain rows (pure Python; Spark-free)
# ---------------------------------------------------------------------------


def _to_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def assemble_fact_sheet(
    *,
    ticker: str,
    cik: str,
    company_name: str,
    period: str,
    panel_rows: list[dict],
    peer_rows: list[dict],
    benchmark_row: dict | None,
    anomaly_rows: list[dict],
    sector_sic_code: str | None = None,
) -> FactSheet:
    """panel_rows: silver facts_panel rows for the focal company (any
    periods; only the focal, prior and year-ago quarters are used).
    peer_rows: gold company_quarter rows for peers in the focal period, each
    with a `ticker` key. benchmark_row: the gold sector_benchmark row for the
    focal period."""
    prior_q, year_ago = shift_period(period, -1), shift_period(period, -4)
    sheet = FactSheet(
        ticker=ticker,
        cik=cik,
        company_name=company_name,
        period=period,
        prior_quarter=prior_q,
        year_ago_quarter=year_ago,
        sector_sic_code=sector_sic_code,
    )
    labels = dict(COMMENTARY_METRICS)
    wanted_periods = (year_ago, prior_q, period)

    # 1. Reported (or silver-derived) values, with lineage.
    for row in panel_rows:
        p = period_label(row["calendar_year"], row["calendar_quarter"])
        if p not in wanted_periods or row["metric"] not in labels or row["value"] is None:
            continue
        sheet.add(
            Fact(
                id=fact_id(ticker, row["metric"], p),
                entity=ticker,
                cik=cik,
                metric=row["metric"],
                label=labels[row["metric"]],
                period=p,
                value=float(row["value"]),
                unit=USD,
                source=Source(
                    adsh=row["adsh"],
                    tag=row["tag"],
                    ddate=_to_str(row["ddate"]),
                    qtrs=int(row["qtrs"]),
                    form=row.get("form"),
                    filed=_to_str(row.get("filed")),
                    derivation=row["derivation"],
                    is_derived=bool(row["is_derived"]),
                ),
            )
        )

    # 2. Margins, computed here from the facts above so their inputs are citable.
    for p in wanted_periods:
        revenue = sheet.get(fact_id(ticker, "revenue", p))
        for ratio_metric, label, numerator_metric in RATIOS:
            numerator = sheet.get(fact_id(ticker, numerator_metric, p))
            if revenue and numerator and revenue.value != 0:
                sheet.add(
                    Fact(
                        id=fact_id(ticker, ratio_metric, p),
                        entity=ticker,
                        cik=cik,
                        metric=ratio_metric,
                        label=label,
                        period=p,
                        value=numerator.value / revenue.value,
                        unit=RATIO,
                        formula=RATIO_FORMULA,
                        inputs=(numerator.id, revenue.id),
                    )
                )

    # 3. QoQ / YoY changes for the focal quarter, and margin moves.
    for suffix, base_period, word in (("qoq", prior_q, "QoQ"), ("yoy", year_ago, "YoY")):
        for m in CHANGE_METRICS:
            cur, base = sheet.get(fact_id(ticker, m, period)), sheet.get(fact_id(ticker, m, base_period))
            if cur and base and base.value != 0:
                sheet.add(
                    Fact(
                        id=fact_id(ticker, f"{m}_{suffix}_pct", period),
                        entity=ticker,
                        cik=cik,
                        metric=f"{m}_{suffix}_pct",
                        label=f"{labels[m]} change {word}",
                        period=period,
                        value=(cur.value - base.value) / abs(base.value),
                        unit=RATIO,
                        formula=PCT_CHANGE,
                        inputs=(cur.id, base.id),
                    )
                )
        for ratio_metric, label, _ in RATIOS:
            cur, base = (
                sheet.get(fact_id(ticker, ratio_metric, period)),
                sheet.get(fact_id(ticker, ratio_metric, base_period)),
            )
            if cur and base:
                sheet.add(
                    Fact(
                        id=fact_id(ticker, f"{ratio_metric}_{suffix}_change", period),
                        entity=ticker,
                        cik=cik,
                        metric=f"{ratio_metric}_{suffix}_change",
                        label=f"{label} change {word} (percentage points)",
                        period=period,
                        value=cur.value - base.value,
                        unit=RATIO,
                        formula=DIFFERENCE,
                        inputs=(cur.id, base.id),
                    )
                )

    # 4. Sector benchmark (medians and quartiles, focal period only).
    if benchmark_row:
        n = benchmark_row.get("filer_count")
        floor = benchmark_row.get("min_quarterly_revenue") or 0
        population = f"SIC {sector_sic_code}, {n} filers" if sector_sic_code else f"{n} filers"
        if floor:
            population += f" with quarterly revenue above {format_value(float(floor), USD)}"
        for ratio_metric, label, _ in RATIOS:
            for stat, stat_label in (("median", "median"), ("p25", "25th percentile"), ("p75", "75th percentile")):
                v = benchmark_row.get(f"{ratio_metric}_{stat}")
                if v is None:
                    continue
                sheet.add(
                    Fact(
                        id=fact_id("SECTOR", f"{ratio_metric}_{stat}", period),
                        entity="SECTOR",
                        cik=None,
                        metric=f"{ratio_metric}_{stat}",
                        label=f"Sector {label} ({stat_label})",
                        period=period,
                        value=float(v),
                        unit=RATIO,
                        note=population,
                    )
                )

    # 5. Peers (focal period only). Peer values come from the gold table.
    for row in peer_rows:
        peer = row["ticker"]
        for ratio_metric in PEER_RATIOS + ["revenue_yoy_pct"]:
            v = row.get(ratio_metric)
            if v is None:
                continue
            label = {
                "gross_margin_pct": "Gross margin",
                "operating_margin_pct": "Operating margin",
                "revenue_yoy_pct": "Revenue change YoY",
            }[ratio_metric]
            sheet.add(
                Fact(
                    id=fact_id(peer, ratio_metric, period),
                    entity=peer,
                    cik=row.get("cik"),
                    metric=ratio_metric,
                    label=f"{peer} {label}",
                    period=period,
                    value=float(v),
                    unit=RATIO,
                    note="peer, from gold company_quarter",
                )
            )

    sheet.anomalies = [{k: _to_str(v) for k, v in a.items()} for a in anomaly_rows if a.get("period") in wanted_periods]
    return sheet


# ---------------------------------------------------------------------------
# Spark-backed builder
# ---------------------------------------------------------------------------


def build_fact_sheet(
    spark: SparkSession,
    ticker: str,
    cik: str,
    period: str,
    peers: dict[str, str] | None = None,
    sector_sic_code: str | None = None,
    data_dir: Path | None = None,
    company_name: str | None = None,
) -> FactSheet:
    """Reads silver/gold outputs and assembles the fact sheet for one
    company-quarter. peers: {ticker: cik}. company_name overrides the SEC's
    registrant name (e.g. "INTEL CORP") for display."""
    from pyspark.sql import functions as F

    data_dir = data_dir or PROJECT_ROOT / "data"
    silver, gold = data_dir / "silver", data_dir / "gold"
    year, quarter = parse_period(period)
    quarter_index = year * 4 + quarter - 1
    peers = peers or {}

    panel = spark.read.parquet(str(silver / "facts_panel")).filter(F.col("cik") == cik)
    panel = panel.withColumn("qi", F.col("calendar_year") * 4 + F.col("calendar_quarter") - 1).filter(
        F.col("qi").isin(quarter_index, quarter_index - 1, quarter_index - 4)
    )
    panel_rows = [r.asDict() for r in panel.collect()]
    if not panel_rows:
        raise LookupError(f"No silver facts for {ticker} (CIK {cik}) around {period}. Has the pipeline run?")

    cq = spark.read.parquet(str(gold / "company_quarter")).filter(
        (F.col("calendar_year") == year) & (F.col("calendar_quarter") == quarter)
    )
    company_name = (
        company_name
        or next((r["name"] for r in cq.filter(F.col("cik") == cik).collect()), None)
        or panel_rows[0]["name"]
    )
    peer_rows = []
    if peers:
        cik_to_ticker = {c: t for t, c in peers.items()}
        for r in cq.filter(F.col("cik").isin(list(cik_to_ticker))).collect():
            peer_rows.append({**r.asDict(), "ticker": cik_to_ticker[r["cik"]]})

    bench_path = gold / "sector_benchmark"
    benchmark_row = None
    if bench_path.exists():
        rows = (
            spark.read.parquet(str(bench_path))
            .filter((F.col("calendar_year") == year) & (F.col("calendar_quarter") == quarter))
            .collect()
        )
        benchmark_row = rows[0].asDict() if rows else None

    anomaly_path = gold / "anomaly_log"
    anomaly_rows = []
    if anomaly_path.exists():
        anomaly_rows = [r.asDict() for r in spark.read.parquet(str(anomaly_path)).filter(F.col("cik") == cik).collect()]

    return assemble_fact_sheet(
        ticker=ticker,
        cik=cik,
        company_name=company_name,
        period=period,
        panel_rows=panel_rows,
        peer_rows=peer_rows,
        benchmark_row=benchmark_row,
        anomaly_rows=anomaly_rows,
        sector_sic_code=sector_sic_code,
    )
