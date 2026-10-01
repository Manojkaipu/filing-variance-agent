import datetime as dt

from pyspark.sql import types as T

from src.anomaly.rules import (
    LOG_COLUMNS,
    check_balance_sheet_identity,
    check_gross_profit_identity,
    check_restatements,
    check_scale_errors,
    check_sign_anomalies,
    check_ytd_consistency,
)

CQ_SCHEMA = T.StructType(
    [
        T.StructField("cik", T.StringType()),
        T.StructField("calendar_year", T.IntegerType()),
        T.StructField("calendar_quarter", T.IntegerType()),
        T.StructField("quarter_index", T.IntegerType()),
        T.StructField("revenue", T.DoubleType()),
        T.StructField("cost_of_revenue", T.DoubleType()),
        T.StructField("gross_profit", T.DoubleType()),
        T.StructField("operating_income", T.DoubleType()),
        T.StructField("net_income", T.DoubleType()),
        T.StructField("research_and_development_expense", T.DoubleType()),
        T.StructField("sga_expense", T.DoubleType()),
        T.StructField("total_assets", T.DoubleType()),
        T.StructField("total_liabilities", T.DoubleType()),
        T.StructField("stockholders_equity", T.DoubleType()),
    ]
)


def cq_row(year, q, revenue=None, cost=None, gp=None, rd=None, assets=None, liabilities=None, equity=None):
    return ("X", year, q, year * 4 + q - 1, revenue, cost, gp, None, None, rd, None, assets, liabilities, equity)


def test_gross_profit_identity_flags_material_mismatch(spark):
    # revenue - cost = 400 but reported gross_profit = 100: 75% off, past 0.5%.
    cq = spark.createDataFrame([cq_row(2023, 1, 1000.0, 600.0, 100.0)], CQ_SCHEMA)
    rows = check_gross_profit_identity(cq, tolerance=0.005).collect()
    assert len(rows) == 1
    assert rows[0]["rule"] == "gross_profit_identity"
    assert rows[0]["period"] == "2023Q1"
    assert list(rows[0].asDict()) == LOG_COLUMNS


def test_gross_profit_identity_allows_small_rounding(spark):
    cq = spark.createDataFrame([cq_row(2023, 1, 1000.0, 600.0, 401.0)], CQ_SCHEMA)  # 0.25% off
    assert check_gross_profit_identity(cq, tolerance=0.005).count() == 0


def test_balance_sheet_identity(spark):
    cq = spark.createDataFrame(
        [
            cq_row(2023, 1, assets=1000.0, liabilities=600.0, equity=400.0),
            cq_row(2023, 2, assets=1000.0, liabilities=600.0, equity=300.0),
        ],
        CQ_SCHEMA,
    )
    assert [r["period"] for r in check_balance_sheet_identity(cq, 0.005).collect()] == ["2023Q2"]


def test_scale_error_flags_100x_jump(spark):
    # Three quarters of ~$18B revenue, then $2T: a thousands-vs-millions mistake.
    # (1.8T against an $18.2B median is only ~99x and correctly stays under.)
    cq = spark.createDataFrame(
        [
            cq_row(2023, 1, 18.0e9),
            cq_row(2023, 2, 18.2e9),
            cq_row(2023, 3, 18.4e9),
            cq_row(2023, 4, 2.0e12),
        ],
        CQ_SCHEMA,
    )
    rows = check_scale_errors(cq, "revenue", multiple=100, min_history=3).collect()
    assert [(r["period"], r["action"]) for r in rows] == [("2023Q4", "flagged_not_auto_corrected")]


def test_scale_error_flags_a_value_reported_in_thousands(spark):
    cq = spark.createDataFrame(
        [
            cq_row(2023, 1, 18.0e9),
            cq_row(2023, 2, 18.2e9),
            cq_row(2023, 3, 18.4e9),
            cq_row(2023, 4, 18.6e6),
        ],
        CQ_SCHEMA,
    )
    assert check_scale_errors(cq, "revenue", multiple=100, min_history=3).count() == 1


def test_scale_error_does_not_flag_normal_growth(spark):
    cq = spark.createDataFrame(
        [
            cq_row(2023, 1, 18.0e9),
            cq_row(2023, 2, 18.2e9),
            cq_row(2023, 3, 18.4e9),
            cq_row(2023, 4, 20.24e9),
        ],
        CQ_SCHEMA,
    )
    assert check_scale_errors(cq, "revenue", multiple=100, min_history=3).count() == 0


def test_sign_anomaly_flags_negative_expense_but_does_not_correct_it(spark):
    cq = spark.createDataFrame([cq_row(2023, 1, rd=-50000.0)], CQ_SCHEMA)
    rows = check_sign_anomalies(cq).filter("metric = 'research_and_development_expense'").collect()
    assert len(rows) == 1
    assert rows[0]["action"] == "flagged_not_auto_corrected"


FACTS_SCHEMA = T.StructType(
    [
        T.StructField("cik", T.StringType()),
        T.StructField("metric", T.StringType()),
        T.StructField("ddate", T.DateType()),
        T.StructField("qtrs", T.IntegerType()),
        T.StructField("value", T.DoubleType()),
        T.StructField("filed", T.DateType()),
    ]
)


def test_restatement_keeps_both_values_and_logs_the_event(spark):
    facts = spark.createDataFrame(
        [
            ("X", "revenue", dt.date(2023, 3, 31), 1, 1000.0, dt.date(2023, 4, 20)),
            # The same fact, reported again later with a materially different value.
            ("X", "revenue", dt.date(2023, 3, 31), 1, 1050.0, dt.date(2023, 7, 20)),
        ],
        FACTS_SCHEMA,
    )
    rows = check_restatements(facts, relative_threshold=0.01).collect()
    assert len(rows) == 1
    assert rows[0]["action"] == "both_values_kept"
    assert rows[0]["period"] == "2023Q1"
    assert "first_reported=1000" in rows[0]["detail"] and "latest=1050" in rows[0]["detail"]


def test_restatement_ignores_immaterial_rounding(spark):
    facts = spark.createDataFrame(
        [
            ("X", "revenue", dt.date(2023, 3, 31), 1, 1000.0, dt.date(2023, 4, 20)),
            ("X", "revenue", dt.date(2023, 3, 31), 1, 1001.0, dt.date(2023, 7, 20)),  # 0.1%
        ],
        FACTS_SCHEMA,
    )
    assert check_restatements(facts, relative_threshold=0.01).count() == 0


def ytd_facts(spark, q3_value):
    return spark.createDataFrame(
        [
            ("X", "revenue", dt.date(2023, 3, 31), 1, 100.0, dt.date(2023, 4, 20)),
            ("X", "revenue", dt.date(2023, 6, 30), 1, 110.0, dt.date(2023, 7, 20)),
            ("X", "revenue", dt.date(2023, 9, 30), 1, q3_value, dt.date(2023, 10, 20)),
            ("X", "revenue", dt.date(2023, 9, 30), 3, 330.0, dt.date(2023, 10, 20)),
            # Prior-year Q4 sits outside the nine-month window and must be ignored.
            ("X", "revenue", dt.date(2022, 12, 31), 1, 999.0, dt.date(2023, 1, 20)),
        ],
        FACTS_SCHEMA,
    )


def test_ytd_consistency_passes_when_quarters_sum_to_ytd(spark):
    assert check_ytd_consistency(ytd_facts(spark, 120.0), tolerance=0.005).count() == 0


def test_ytd_consistency_flags_quarters_that_do_not_sum_to_ytd(spark):
    rows = check_ytd_consistency(ytd_facts(spark, 0.12), tolerance=0.005).collect()
    assert [(r["rule"], r["period"]) for r in rows] == [("ytd_consistency", "2023Q3")]
