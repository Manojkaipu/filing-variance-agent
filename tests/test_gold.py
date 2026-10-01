import datetime as dt

import pytest
from pyspark.sql import types as T

from src.pipeline.gold import add_period_over_period_changes, build_sector_benchmark, pivot_company_quarter

CQ_SCHEMA = T.StructType(
    [
        T.StructField("cik", T.StringType()),
        T.StructField("calendar_year", T.IntegerType()),
        T.StructField("calendar_quarter", T.IntegerType()),
        T.StructField("revenue", T.DoubleType()),
        T.StructField("gross_profit", T.DoubleType()),
        T.StructField("operating_income", T.DoubleType()),
        T.StructField("net_income", T.DoubleType()),
    ]
)


def changes(spark, rows):
    out = add_period_over_period_changes(spark.createDataFrame(rows, CQ_SCHEMA)).collect()
    return {(r["calendar_year"], r["calendar_quarter"]): r for r in out}


def test_qoq_and_yoy_use_the_companys_own_quarter_sequence(spark):
    rows = changes(
        spark,
        [
            ("X", 2023, 1, 100.0, None, None, None),
            ("X", 2023, 2, 110.0, None, None, None),
            ("X", 2023, 3, 120.0, None, None, None),
            ("X", 2023, 4, 130.0, None, None, None),
            ("X", 2024, 1, 150.0, None, None, None),
        ],
    )
    last = rows[(2024, 1)]
    assert last["revenue_qoq_pct"] == pytest.approx((150.0 - 130.0) / 130.0)  # across the year boundary
    assert last["revenue_yoy_pct"] == pytest.approx((150.0 - 100.0) / 100.0)
    assert rows[(2023, 1)]["revenue_qoq_pct"] is None
    assert rows[(2023, 1)]["revenue_yoy_pct"] is None


def test_missing_quarter_yields_null_qoq_instead_of_comparing_wrong_periods(spark):
    # 2023Q2 is missing. 2023Q3's QoQ must be null, not computed against 2023Q1.
    rows = changes(
        spark,
        [
            ("X", 2023, 1, 100.0, None, None, None),
            ("X", 2023, 3, 120.0, None, None, None),
            ("X", 2024, 3, 132.0, None, None, None),
        ],
    )
    assert rows[(2023, 3)]["revenue_qoq_pct"] is None
    # YoY still works across the gap: 2024Q3 vs 2023Q3.
    assert rows[(2024, 3)]["revenue_yoy_pct"] == pytest.approx(0.10)


def test_changes_never_cross_companies(spark):
    rows = add_period_over_period_changes(
        spark.createDataFrame(
            [
                ("X", 2023, 1, 100.0, None, None, None),
                ("Y", 2023, 2, 500.0, None, None, None),
            ],
            CQ_SCHEMA,
        )
    ).collect()
    assert all(r["revenue_qoq_pct"] is None for r in rows)


def test_sector_benchmark_only_includes_the_configured_sic_code(spark):
    schema = (
        "cik string, sic string, calendar_year int, calendar_quarter int, revenue double, "
        "gross_margin_pct double, operating_margin_pct double, rd_intensity_pct double, net_margin_pct double"
    )
    cq = spark.createDataFrame(
        [
            ("A", "3674", 2024, 1, 10.0, 0.40, 0.10, 0.20, 0.08),
            ("B", "3674", 2024, 1, 10.0, 0.50, 0.20, 0.15, 0.15),
            ("C", "3674", 2024, 1, 10.0, 0.60, 0.30, 0.10, 0.25),
            ("R", "5331", 2024, 1, 90.0, 0.25, 0.04, 0.00, 0.03),  # a retailer
        ],
        schema,
    )
    row = build_sector_benchmark(cq, "3674").collect()[0]
    assert row["filer_count"] == 3
    assert row["gross_margin_pct_median"] == pytest.approx(0.50)
    assert row["gross_margin_pct_p25"] == pytest.approx(0.40)
    assert row["gross_margin_pct_p75"] == pytest.approx(0.60)
    assert row["sic_code"] == "3674"


def test_revenue_floor_excludes_small_filers_and_is_recorded(spark):
    # Without a floor, a handful of micro-cap filers drag the median far from
    # anything a large-cap can be compared against. Measured on real SIC 3674
    # data, the 2024Q4 operating-margin median was 0.1% across all filers and
    # 21.7% across those above $500M.
    schema = (
        "cik string, sic string, calendar_year int, calendar_quarter int, revenue double, "
        "gross_margin_pct double, operating_margin_pct double, rd_intensity_pct double, net_margin_pct double"
    )
    cq = spark.createDataFrame(
        [
            ("BIG1", "3674", 2024, 4, 5.0e9, 0.50, 0.30, 0.15, 0.25),
            ("BIG2", "3674", 2024, 4, 2.0e9, 0.45, 0.20, 0.15, 0.16),
            ("BIG3", "3674", 2024, 4, 1.0e9, 0.40, 0.10, 0.15, 0.08),
            ("TINY", "3674", 2024, 4, 1.0e6, -0.20, -1.79, 0.90, -2.00),
        ],
        schema,
    )
    # One micro-cap is enough to pull the median off the large-cap cohort.
    unfiltered = build_sector_benchmark(cq, "3674").collect()[0]
    assert unfiltered["filer_count"] == 4
    assert unfiltered["operating_margin_pct_median"] == pytest.approx(0.10)

    floored = build_sector_benchmark(cq, "3674", min_revenue=50e6).collect()[0]
    assert floored["filer_count"] == 3
    assert floored["operating_margin_pct_median"] == pytest.approx(0.20)
    # The population is recorded, so a median is never read out of context.
    assert floored["min_quarterly_revenue"] == pytest.approx(50e6)


def test_pivot_keeps_one_row_per_company_quarter_across_a_rename(spark):
    schema = (
        "cik string, name string, sic string, metric string, value double, calendar_year int, "
        "calendar_quarter int, filed date"
    )
    panel = spark.createDataFrame(
        [
            ("X", "OLD NAME INC", "3674", "revenue", 100.0, 2023, 1, dt.date(2023, 5, 1)),
            ("X", "NEW NAME INC", "3674", "net_income", 10.0, 2023, 1, dt.date(2024, 5, 1)),
        ],
        schema,
    )
    rows = pivot_company_quarter(panel).collect()
    assert len(rows) == 1
    assert rows[0]["name"] == "NEW NAME INC"
    assert (rows[0]["revenue"], rows[0]["net_income"]) == (100.0, 10.0)
