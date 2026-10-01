import datetime as dt

import pytest
from pyspark.sql import functions as F
from pyspark.sql import types as T

from src.pipeline.silver import (
    PANEL_COLUMNS,
    add_derived_gross_profit,
    calendar_quarter,
    derive_q4_facts,
    extract_instant_facts,
    extract_quarterly_facts,
    latest_filed_facts,
    resolve_one_value_per_quarter,
    select_preferred_tags,
)

FACTS_SCHEMA = T.StructType(
    [
        T.StructField("adsh", T.StringType()),
        T.StructField("cik", T.StringType()),
        T.StructField("name", T.StringType()),
        T.StructField("sic", T.StringType()),
        T.StructField("tag", T.StringType()),
        T.StructField("metric", T.StringType()),
        T.StructField("priority", T.IntegerType()),
        T.StructField("value", T.DoubleType()),
        T.StructField("ddate", T.DateType()),
        T.StructField("qtrs", T.IntegerType()),
        T.StructField("form", T.StringType()),
        T.StructField("fy", T.StringType()),
        T.StructField("fp", T.StringType()),
        T.StructField("filed", T.DateType()),
    ]
)
INTEL = ("0000050863", "Intel Corporation", "3674")
REV_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"


def fact(
    adsh, metric, value, ddate, qtrs, filed, *, fy="2023", fp="FY", form="10-K", tag=REV_TAG, priority=0, company=INTEL
):
    cik, name, sic = company
    return (adsh, cik, name, sic, tag, metric, priority, value, ddate, qtrs, form, fy, fp, filed)


def make_facts(spark, rows):
    return spark.createDataFrame(rows, FACTS_SCHEMA)


# --- Q4 derivation -------------------------------------------------------------


def test_q4_is_derived_as_annual_minus_nine_month_ytd(spark):
    # Real-shaped numbers: $54.228B annual revenue and $38.812B through nine
    # months -> Q4 should be $15.416B.
    facts = make_facts(
        spark,
        [
            fact("10K-2023", "revenue", 54228000000.0, dt.date(2023, 12, 30), 4, dt.date(2024, 1, 25)),
            fact(
                "10Q-2023Q3",
                "revenue",
                38812000000.0,
                dt.date(2023, 9, 30),
                3,
                dt.date(2023, 10, 26),
                fp="Q3",
                form="10-Q",
            ),
        ],
    )
    row = derive_q4_facts(facts).collect()[0]

    assert row["value"] == 54228000000.0 - 38812000000.0
    assert row["qtrs"] == 1  # the result is a one-quarter duration
    assert row["is_derived"] is True
    assert row["derivation"] == "annual_minus_ytd9"
    assert row["adsh"] == "10K-2023|10Q-2023Q3"  # lineage cites both filings
    assert (row["calendar_year"], row["calendar_quarter"]) == (2023, 4)


def test_q4_derivation_is_not_fooled_by_comparative_columns(spark):
    # A 10-K reports this year's AND last year's annual totals, and both carry
    # the filing's fy=2023. Matching on fy would subtract the wrong year's
    # nine-month figure; matching on period end dates gets each year right.
    facts = make_facts(
        spark,
        [
            fact("10K-2023", "revenue", 54.0e9, dt.date(2023, 12, 30), 4, dt.date(2024, 1, 25)),
            fact("10K-2023", "revenue", 63.0e9, dt.date(2022, 12, 31), 4, dt.date(2024, 1, 25)),
            fact("10Q-2023Q3", "revenue", 38.8e9, dt.date(2023, 9, 30), 3, dt.date(2023, 10, 26), fp="Q3"),
            fact("10Q-2023Q3", "revenue", 48.9e9, dt.date(2022, 10, 1), 3, dt.date(2023, 10, 26), fp="Q3"),
        ],
    )
    rows = {r["calendar_year"]: r["value"] for r in derive_q4_facts(facts).collect()}
    assert rows == {2023: pytest.approx(54.0e9 - 38.8e9), 2022: pytest.approx(63.0e9 - 48.9e9)}


def test_q4_not_derived_when_ytd_side_is_missing(spark):
    # Silver must not guess a Q4 value, or treat a missing side as zero.
    facts = make_facts(
        spark,
        [
            fact("10K-2023", "revenue", 54228000000.0, dt.date(2023, 12, 30), 4, dt.date(2024, 1, 25)),
        ],
    )
    assert derive_q4_facts(facts).count() == 0


# --- calendar alignment ----------------------------------------------------------


@pytest.mark.parametrize(
    "period_end,expected",
    [
        (dt.date(2023, 12, 30), (2023, 4)),  # Intel, December year-end
        (dt.date(2024, 1, 28), (2023, 4)),  # NVIDIA fiscal Q4 covers Nov-Jan -> calendar Q4
        (dt.date(2024, 4, 28), (2024, 1)),  # NVIDIA fiscal Q1 covers Feb-Apr -> calendar Q1
        (dt.date(2023, 8, 31), (2023, 3)),  # Micron, late-August quarter end
        (dt.date(2025, 1, 4), (2024, 4)),  # 53-week year ending in January
        (dt.date(2023, 4, 1), (2023, 1)),  # 52-week Q1 ending April 1
    ],
)
def test_calendar_quarter_uses_the_period_midpoint(spark, period_end, expected):
    df = spark.createDataFrame([(period_end,)], "ddate date")
    year, quarter = calendar_quarter(F.col("ddate"))
    row = df.select(year.alias("y"), quarter.alias("q")).collect()[0]
    assert (row["y"], row["q"]) == expected


def test_quarterly_and_instant_facts_are_bucketed(spark):
    facts = make_facts(
        spark,
        [
            fact("10K", "revenue", 22103000000.0, dt.date(2024, 1, 28), 1, dt.date(2024, 2, 21), fy="2024"),
            fact("10K", "total_assets", 191572000000.0, dt.date(2023, 12, 30), 0, dt.date(2024, 1, 25), tag="Assets"),
        ],
    )
    q = extract_quarterly_facts(facts).collect()[0]
    assert (q["calendar_year"], q["calendar_quarter"], q["derivation"]) == (2023, 4, "reported_quarterly")
    i = extract_instant_facts(facts).collect()[0]
    assert (i["calendar_year"], i["calendar_quarter"], i["derivation"]) == (2023, 4, "instant")


# --- tag priority and restatement history -----------------------------------------


def test_preferred_tag_wins_within_a_filing_but_filings_are_not_collapsed(spark):
    d, filed = dt.date(2023, 3, 31), dt.date(2023, 4, 27)
    facts = make_facts(
        spark,
        [
            fact("Q1-2023", "revenue", 1000.0, d, 1, filed, tag=REV_TAG, priority=0),
            fact("Q1-2023", "revenue", 999.0, d, 1, filed, tag="Revenues", priority=2),
            # The same period, re-reported as a comparative a year later.
            fact("Q1-2024", "revenue", 1050.0, d, 1, dt.date(2024, 4, 25), tag="Revenues", priority=2),
        ],
    )
    kept = {(r["adsh"], r["tag"]) for r in select_preferred_tags(facts).collect()}
    assert kept == {("Q1-2023", REV_TAG), ("Q1-2024", "Revenues")}


def test_panel_uses_the_latest_filed_value(spark):
    d = dt.date(2023, 3, 31)
    facts = make_facts(
        spark,
        [
            fact("Q1-2023", "revenue", 1000.0, d, 1, dt.date(2023, 4, 27)),
            fact("Q1-2024", "revenue", 1050.0, d, 1, dt.date(2024, 4, 25)),
        ],
    )
    rows = latest_filed_facts(facts).collect()
    assert [(r["adsh"], r["value"]) for r in rows] == [("Q1-2024", 1050.0)]


# --- panel-level rules -------------------------------------------------------------

PANEL_SCHEMA = T.StructType(
    [
        T.StructField(c, t)
        for c, t in zip(
            PANEL_COLUMNS,
            [
                T.StringType(),
                T.StringType(),
                T.StringType(),
                T.StringType(),
                T.DoubleType(),
                T.DateType(),
                T.IntegerType(),
                T.StringType(),
                T.StringType(),
                T.StringType(),
                T.DateType(),
                T.IntegerType(),
                T.IntegerType(),
                T.BooleanType(),
                T.StringType(),
                T.StringType(),
                T.StringType(),
            ],
            strict=True,
        )
    ]
)


def panel_row(
    metric, value, *, is_derived=False, derivation="reported_quarterly", adsh="A1", tag="T", ddate=None, filed=None
):
    ddate = ddate or dt.date(2023, 3, 31)
    return (
        "X",
        "Company X",
        "3674",
        metric,
        value,
        ddate,
        1,
        "10-Q",
        "2023",
        "Q1",
        filed or dt.date(2023, 5, 1),
        2023,
        1,
        is_derived,
        derivation,
        adsh,
        tag,
    )


def test_gross_profit_is_derived_when_not_directly_reported(spark):
    panel = spark.createDataFrame(
        [
            panel_row("revenue", 1000.0, adsh="A1", tag="Revenues"),
            panel_row("cost_of_revenue", 600.0, adsh="A1", tag="CostOfRevenue"),
        ],
        PANEL_SCHEMA,
    )
    rows = add_derived_gross_profit(panel).filter("metric = 'gross_profit'").collect()

    assert len(rows) == 1
    gp = rows[0]
    assert gp["value"] == 400.0 and gp["is_derived"] is True
    assert gp["tag"] == "Revenues|CostOfRevenue" and gp["adsh"] == "A1|A1"
    assert gp["name"] == "Company X" and gp["ddate"] == dt.date(2023, 3, 31)


def test_gross_profit_not_re_derived_when_already_reported(spark):
    # The disclosed value differs slightly from revenue - cost (rounding in the
    # filing itself); silver must not overwrite it with a derived one.
    panel = spark.createDataFrame(
        [
            panel_row("revenue", 1000.0),
            panel_row("cost_of_revenue", 600.0),
            panel_row("gross_profit", 401.0),
        ],
        PANEL_SCHEMA,
    )
    rows = add_derived_gross_profit(panel).filter("metric = 'gross_profit'").collect()
    assert [(r["value"], r["is_derived"]) for r in rows] == [(401.0, False)]


def test_reported_quarter_beats_derived_quarter(spark):
    # Some filers tag a standalone Q4 in the 10-K as well; keep the disclosed one.
    panel = spark.createDataFrame(
        [
            panel_row("revenue", 1000.0, is_derived=True, derivation="annual_minus_ytd9"),
            panel_row("revenue", 1001.0),
        ],
        PANEL_SCHEMA,
    )
    rows = resolve_one_value_per_quarter(panel).collect()
    assert [(r["value"], r["is_derived"]) for r in rows] == [(1001.0, False)]
