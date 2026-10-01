"""
A small, hand-checkable fact sheet for a fictional company (ACME) and the
matching "as filed" ledger, for tests that don't need Spark.

Focal quarter 2024Q4 (a derived Q4: annual minus nine-month YTD).
    revenue          2023Q4 10,000M   2024Q3 10,500M   2024Q4 11,000M
    cost_of_revenue         6,000M           6,200M           6,270M
    => revenue YoY +10.0%, QoQ +4.76%; gross margin 40.0% -> 43.0% (+300 bps)
"""

import datetime as dt

from src.agent.facts import assemble_fact_sheet
from src.agent.verifier import InMemoryLedger

CIK = "0000000001"
TAGS = {
    "revenue": "Revenues",
    "cost_of_revenue": "CostOfRevenue",
    "research_and_development_expense": "ResearchAndDevelopmentExpense",
    "sga_expense": "SellingGeneralAndAdministrativeExpense",
    "operating_income": "OperatingIncomeLoss",
    "net_income": "NetIncomeLoss",
}
M = 1e6
VALUES = {
    "2023Q4": dict(
        revenue=10_000 * M,
        cost_of_revenue=6_000 * M,
        research_and_development_expense=2_000 * M,
        sga_expense=1_000 * M,
        operating_income=1_000 * M,
        net_income=800 * M,
    ),
    "2024Q3": dict(
        revenue=10_500 * M,
        cost_of_revenue=6_200 * M,
        research_and_development_expense=2_100 * M,
        sga_expense=1_050 * M,
        operating_income=1_150 * M,
        net_income=900 * M,
    ),
    "2024Q4": dict(
        revenue=11_000 * M,
        cost_of_revenue=6_270 * M,
        research_and_development_expense=2_200 * M,
        sga_expense=1_100 * M,
        operating_income=1_430 * M,
        net_income=1_150 * M,
    ),
}
# Accession numbers: annual 10-Ks and Q3 10-Qs.
A23, Q323, A24, Q324 = "0000000001-24-000004", "0000000001-23-000003", "0000000001-25-000004", "0000000001-24-000003"
YTD9 = {
    "2023Q4": {m: 3 * v * 0.95 for m, v in VALUES["2023Q4"].items()},
    "2024Q4": {m: 3 * v for m, v in VALUES["2024Q3"].items()},
}


def _row(period, metric, value, *, adsh, tag, ddate, derivation, is_derived):
    year, q = int(period[:4]), int(period[-1])
    return dict(
        cik=CIK,
        name="ACME CORP",
        sic="3674",
        metric=metric,
        value=value,
        ddate=ddate,
        qtrs=1,
        form="10-K" if q == 4 else "10-Q",
        fy=str(year),
        fp=f"Q{q}",
        filed=ddate + dt.timedelta(days=40),
        calendar_year=year,
        calendar_quarter=q,
        is_derived=is_derived,
        derivation=derivation,
        adsh=adsh,
        tag=tag,
    )


def panel_rows():
    rows = []
    for m, tag in TAGS.items():
        rows.append(
            _row(
                "2023Q4",
                m,
                VALUES["2023Q4"][m],
                adsh=f"{A23}|{Q323}",
                tag=f"{tag}|{tag}",
                ddate=dt.date(2023, 12, 31),
                derivation="annual_minus_ytd9",
                is_derived=True,
            )
        )
        rows.append(
            _row(
                "2024Q3",
                m,
                VALUES["2024Q3"][m],
                adsh=Q324,
                tag=tag,
                ddate=dt.date(2024, 9, 30),
                derivation="reported_quarterly",
                is_derived=False,
            )
        )
        rows.append(
            _row(
                "2024Q4",
                m,
                VALUES["2024Q4"][m],
                adsh=f"{A24}|{Q324}",
                tag=f"{tag}|{tag}",
                ddate=dt.date(2024, 12, 31),
                derivation="annual_minus_ytd9",
                is_derived=True,
            )
        )
    for period in VALUES:
        v = VALUES[period]
        rev = next(
            r for r in rows if r["metric"] == "revenue" and r["fp"] == f"Q{period[-1]}" and r["fy"] == period[:4]
        )
        cost = next(
            r for r in rows if r["metric"] == "cost_of_revenue" and r["fy"] == rev["fy"] and r["fp"] == rev["fp"]
        )
        rows.append(
            {
                **rev,
                "metric": "gross_profit",
                "value": v["revenue"] - v["cost_of_revenue"],
                "adsh": f"{rev['adsh']}|{cost['adsh']}",
                "tag": f"{rev['tag']}|{cost['tag']}",
                "derivation": "revenue_minus_cost_of_revenue",
                "is_derived": True,
            }
        )
    return rows


def ledger_rows():
    rows = []
    for m, tag in TAGS.items():
        rows.append((Q324, tag, "2024-09-30", 1, VALUES["2024Q3"][m]))
        rows.append((Q324, tag, "2024-09-30", 3, YTD9["2024Q4"][m]))
        rows.append((A24, tag, "2024-12-31", 4, YTD9["2024Q4"][m] + VALUES["2024Q4"][m]))
        rows.append((Q323, tag, "2023-09-30", 3, YTD9["2023Q4"][m]))
        rows.append((A23, tag, "2023-12-31", 4, YTD9["2023Q4"][m] + VALUES["2023Q4"][m]))
    return rows


def make_sheet(anomalies=None):
    return assemble_fact_sheet(
        ticker="ACME",
        cik=CIK,
        company_name="Acme Corp",
        period="2024Q4",
        panel_rows=panel_rows(),
        peer_rows=[
            dict(
                ticker="PEER1", cik="0000000002", gross_margin_pct=0.50, operating_margin_pct=0.20, revenue_yoy_pct=0.08
            ),
            dict(
                ticker="PEER2",
                cik="0000000003",
                gross_margin_pct=0.38,
                operating_margin_pct=0.10,
                revenue_yoy_pct=-0.02,
            ),
        ],
        benchmark_row=dict(
            filer_count=12,
            gross_margin_pct_median=0.45,
            gross_margin_pct_p25=0.38,
            gross_margin_pct_p75=0.55,
            operating_margin_pct_median=0.15,
        ),
        anomaly_rows=anomalies
        if anomalies is not None
        else [
            dict(
                rule="restatement_or_recast",
                cik=CIK,
                metric="revenue",
                period="2023Q4",
                detail="first_reported=... latest=...",
                action="both_values_kept",
            ),
            dict(rule="scale_error", cik=CIK, metric="revenue", period="2019Q1", detail="old", action="flagged"),
        ],
        sector_sic_code="3674",
    )


def make_ledger(overrides=None):
    rows = ledger_rows()
    for (adsh, tag, ddate, qtrs), value in (overrides or {}).items():
        rows = [r for r in rows if r[:4] != (adsh, tag, ddate, qtrs)] + [(adsh, tag, ddate, qtrs, value)]
    return InMemoryLedger(rows)
