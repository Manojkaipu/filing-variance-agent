"""
Writes a small, fully fictional set of SEC Financial Statement Data Sets
(sub.txt / num.txt / tag.txt, tab-separated, in data/raw/<quarter>/ folders)
so the whole system — pipeline, agent, verifier, review queue — can run end
to end without network access or an EDGAR download.

Every company, ticker and number here is made up. The data is shaped to
exercise the cases the pipeline has to get right on real filings:

  - DEMO   December fiscal year; its FY2023 Q4 is later RESTATED in the FY2024
           10-K comparatives (restatement detection, latest-value selection).
  - ALPHA  December fiscal year; tags revenue as `Revenues` and reports
           `GrossProfit` directly (tag priority, disclosed vs derived GP).
  - BETA   fiscal year ending late January, like NVIDIA (calendarization:
           the quarter ending 2024-01-28 belongs to calendar 2023Q4).
  - GAMMA  52/53-week year whose FY2024 ends 2025-01-04 (a month-of-period-end
           rule would wrongly put that Q4 in 2025Q1).
  - DELTA  a retailer (SIC 5331) that must NOT enter the semiconductor sector
           benchmark; one 10-Q reports revenue in thousands (scale error).

Each 10-Q/10-K carries prior-year comparative columns, as real filings do,
so the same period appears in several filings. One quarter folder uses the
older num.txt column layout (no `segments` column, different order) to
exercise header-based parsing in bronze.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

from src.pipeline.bronze import SUB_SCHEMA, TAG_SCHEMA

D = dt.date
NUM_COLUMNS_NEW = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value", "footnote"]
NUM_COLUMNS_OLD = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "value", "footnote"]
OLD_LAYOUT_QUARTERS = {"2023q2"}
VERSION = "us-gaap/2023"

INCOME_METRICS = [
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_and_development_expense",
    "sga_expense",
    "operating_income",
    "net_income",
]


@dataclass
class Company:
    ticker: str
    cik: int
    name: str
    sic: str
    fye: str  # MMDD
    quarter_ends: dict[int, list[dt.date]]  # fiscal year -> four quarter-end dates
    base_revenue: float
    growth: float
    gross_margin: float
    rd_pct: float
    sga_pct: float
    tags: dict[str, str]  # metric -> XBRL tag this filer uses


def _calendar_year_ends(fy: int) -> list[dt.date]:
    return [D(fy, 3, 31), D(fy, 6, 30), D(fy, 9, 30), D(fy, 12, 31)]


STANDARD_TAGS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "cost_of_revenue": "CostOfRevenue",
    "research_and_development_expense": "ResearchAndDevelopmentExpense",
    "sga_expense": "SellingGeneralAndAdministrativeExpense",
    "operating_income": "OperatingIncomeLoss",
    "net_income": "NetIncomeLoss",
    "total_assets": "Assets",
    "total_liabilities": "Liabilities",
    "stockholders_equity": "StockholdersEquity",
}

COMPANIES = [
    Company(
        "DEMO",
        900001,
        "Demo Silicon Corp",
        "3674",
        "1231",
        {fy: _calendar_year_ends(fy) for fy in (2022, 2023, 2024)},
        base_revenue=12.0e9,
        growth=0.012,
        gross_margin=0.41,
        rd_pct=0.24,
        sga_pct=0.10,
        tags=STANDARD_TAGS,
    ),
    Company(
        "ALPHA",
        900002,
        "Alpha Wafer Inc",
        "3674",
        "1231",
        {fy: _calendar_year_ends(fy) for fy in (2022, 2023, 2024)},
        base_revenue=5.5e9,
        growth=0.035,
        gross_margin=0.50,
        rd_pct=0.22,
        sga_pct=0.08,
        tags={**STANDARD_TAGS, "revenue": "Revenues", "gross_profit": "GrossProfit"},
    ),
    Company(
        "BETA",
        900003,
        "Beta Logic Corp",
        "3674",
        "0126",
        {
            2023: [D(2022, 5, 1), D(2022, 7, 31), D(2022, 10, 30), D(2023, 1, 29)],
            2024: [D(2023, 4, 30), D(2023, 7, 30), D(2023, 10, 29), D(2024, 1, 28)],
            2025: [D(2024, 4, 28), D(2024, 7, 28), D(2024, 10, 27), D(2025, 1, 26)],
        },
        base_revenue=7.0e9,
        growth=0.08,
        gross_margin=0.66,
        rd_pct=0.18,
        sga_pct=0.05,
        tags={**STANDARD_TAGS, "gross_profit": "GrossProfit"},
    ),
    Company(
        "GAMMA",
        900004,
        "Gamma Analog Inc",
        "3674",
        "0104",
        {
            2022: [D(2022, 4, 2), D(2022, 7, 2), D(2022, 10, 1), D(2022, 12, 31)],
            2023: [D(2023, 4, 1), D(2023, 7, 1), D(2023, 9, 30), D(2023, 12, 30)],
            2024: [D(2024, 3, 30), D(2024, 6, 29), D(2024, 9, 28), D(2025, 1, 4)],
        },
        base_revenue=2.4e9,
        growth=0.005,
        gross_margin=0.58,
        rd_pct=0.15,
        sga_pct=0.12,
        tags=STANDARD_TAGS,
    ),
    Company(
        "DELTA",
        900005,
        "Delta Retail Co",
        "5331",
        "1231",
        {fy: _calendar_year_ends(fy) for fy in (2022, 2023, 2024)},
        base_revenue=30.0e9,
        growth=0.01,
        gross_margin=0.28,
        rd_pct=0.0,
        sga_pct=0.21,
        tags={k: v for k, v in STANDARD_TAGS.items() if k != "research_and_development_expense"},
    ),
]

# DEMO restates its FY2023 fourth quarter (+4% across the income statement);
# filings made on or after this date carry the restated figures.
RESTATEMENT = ("DEMO", 2023, 3, D(2024, 6, 1), 1.04)
# DELTA's FY2024 Q2 10-Q reports current-quarter revenue in thousands.
SCALE_ERROR = ("DELTA", 2024, 1)


def _round_m(x: float) -> float:
    return float(round(x / 1e6) * 1e6)


def quarter_values(c: Company, fy: int, q: int, as_of: dt.date) -> dict[str, float]:
    """Income-statement values for fiscal quarter q (0-based) as reported in a
    filing made on `as_of`."""
    years = sorted(c.quarter_ends)
    i = years.index(fy) * 4 + q
    season = [0.95, 0.98, 1.02, 1.05][q]
    rev = _round_m(c.base_revenue * (1 + c.growth) ** i * season)
    gm = c.gross_margin + 0.015 * math.sin(i)
    cost = _round_m(rev * (1 - gm))
    gp = rev - cost
    rd = _round_m(rev * c.rd_pct)
    sga = _round_m(rev * (c.sga_pct + 0.004 * math.cos(i)))
    op = gp - rd - sga
    net = _round_m(op * 0.82)
    v = dict(
        revenue=rev,
        cost_of_revenue=cost,
        gross_profit=gp,
        research_and_development_expense=rd,
        sga_expense=sga,
        operating_income=op,
        net_income=net,
    )
    ticker, r_fy, r_q, effective, mult = RESTATEMENT
    if c.ticker == ticker and (fy, q) == (r_fy, r_q) and as_of >= effective:
        v = {k: _round_m(x * mult) for k, x in v.items()}
    return v


def balance_values(c: Company, fy: int, q: int) -> dict[str, float]:
    i = sorted(c.quarter_ends).index(fy) * 4 + q
    assets = _round_m(c.base_revenue * 6 * (1 + 0.01 * i))
    liabilities = _round_m(assets * 0.45)
    return dict(total_assets=assets, total_liabilities=liabilities, stockholders_equity=assets - liabilities)


def _sum(values: list[dict[str, float]]) -> dict[str, float]:
    return {k: sum(v[k] for v in values) for k in values[0]}


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _fsds_quarter(d: dt.date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def build_filings() -> dict[str, dict[str, list[dict]]]:
    """Returns {fsds_quarter: {"sub": [...], "num": [...]}}."""
    out: dict[str, dict[str, list[dict]]] = {}
    for c in COMPANIES:
        years = sorted(c.quarter_ends)
        for fy in years[1:]:  # the first year exists only as comparatives
            for q in range(4):
                period_end = c.quarter_ends[fy][q]
                is_10k = q == 3
                filed = period_end + dt.timedelta(days=55 if is_10k else 35)
                seq = years.index(fy) * 10 + q + 1
                adsh = f"{c.cik:010d}-{filed.year % 100:02d}-{seq:06d}"
                prior_fy = years[years.index(fy) - 1]
                prior_end = c.quarter_ends[prior_fy]
                cur = [quarter_values(c, fy, k, filed) for k in range(4)]
                prev = [quarter_values(c, prior_fy, k, filed) for k in range(4)]

                # (values, period end, qtrs) column blocks, as a filing presents them.
                blocks: list[tuple[dict[str, float], dt.date, int]] = []
                if is_10k:
                    blocks += [(_sum(cur), period_end, 4), (_sum(prev), prior_end[3], 4)]
                else:
                    this_q = dict(cur[q])
                    if (c.ticker, fy, q) == SCALE_ERROR:
                        this_q["revenue"] = this_q["revenue"] / 1000
                    blocks += [(this_q, period_end, 1), (prev[q], prior_end[q], 1)]
                    if q >= 1:
                        blocks += [(_sum(cur[: q + 1]), period_end, q + 1), (_sum(prev[: q + 1]), prior_end[q], q + 1)]
                blocks += [(balance_values(c, fy, q), period_end, 0), (balance_values(c, prior_fy, 3), prior_end[3], 0)]
                facts = [
                    (c.tags[metric], ddate, qtrs, value)
                    for values, ddate, qtrs in blocks
                    for metric, value in values.items()
                    if metric in c.tags
                ]

                bucket = out.setdefault(_fsds_quarter(filed), {"sub": [], "num": []})
                bucket["sub"].append(
                    {
                        "adsh": adsh,
                        "cik": str(c.cik),
                        "name": c.name.upper(),
                        "sic": c.sic,
                        "countryba": "US",
                        "fye": c.fye,
                        "form": "10-K" if is_10k else "10-Q",
                        "period": _ymd(period_end),
                        "fy": str(fy),
                        "fp": "FY" if is_10k else f"Q{q + 1}",
                        "filed": _ymd(filed),
                        "prevrpt": "0",
                        "detail": "1",
                        "nciks": "1",
                    }
                )
                for tag, ddate, qtrs, value in facts:
                    bucket["num"].append(
                        {
                            "adsh": adsh,
                            "tag": tag,
                            "version": VERSION,
                            "ddate": _ymd(ddate),
                            "qtrs": str(qtrs),
                            "uom": "USD",
                            "value": f"{value:.0f}" if value == int(value) else f"{value:.4f}",
                        }
                    )
                # A dimensional (segment) fact that silver must ignore.
                bucket["num"].append(
                    {
                        "adsh": adsh,
                        "tag": c.tags["revenue"],
                        "version": VERSION,
                        "ddate": _ymd(period_end),
                        "qtrs": "1",
                        "uom": "USD",
                        "segments": "BusinessSegments=ClientComputing;",
                        "value": "1000000",
                    }
                )
    return out


def _write_tsv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(columns) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(col, "") or "") for col in columns) + "\n")


def write_raw(raw_dir: Path) -> list[str]:
    """Writes the synthetic FSDS files. Returns the quarter folders written."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    tag_rows = [
        {
            "tag": t,
            "version": VERSION,
            "custom": "0",
            "abstract": "0",
            "datatype": "monetary",
            "iord": "I" if t in ("Assets", "Liabilities", "StockholdersEquity") else "D",
        }
        for t in sorted({t for c in COMPANIES for t in c.tags.values()})
    ]
    written = []
    for quarter, files in sorted(build_filings().items()):
        qdir = raw_dir / quarter
        qdir.mkdir(exist_ok=True)
        _write_tsv(qdir / "sub.txt", [f.name for f in SUB_SCHEMA.fields], files["sub"])
        num_cols = NUM_COLUMNS_OLD if quarter in OLD_LAYOUT_QUARTERS else NUM_COLUMNS_NEW
        num_rows = files["num"]
        if quarter in OLD_LAYOUT_QUARTERS:
            num_rows = [r for r in num_rows if not r.get("segments")]  # old releases had no dimensional rows
        _write_tsv(qdir / "num.txt", num_cols, num_rows)
        _write_tsv(qdir / "tag.txt", [f.name for f in TAG_SCHEMA.fields], tag_rows)
        written.append(quarter)
    return written


def ticker_map() -> dict[str, str]:
    return {c.ticker: f"{c.cik:010d}" for c in COMPANIES}
