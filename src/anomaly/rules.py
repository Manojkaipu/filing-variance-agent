"""
Anomaly detection over the silver facts panel. Every finding is written as a
row to an anomaly_log table with enough detail to explain (a) what looked
wrong, (b) what was done about it, and (c) why. "Corrected everything
automatically" is not the goal — knowing what NOT to auto-correct is the
actual skill this module demonstrates.

Each rule is a pure function: (facts or company-quarter table) -> anomaly
log rows with the columns in LOG_COLUMNS. Keeping them separate and
composable makes each one independently testable, and makes it easy to add a
rule without touching the others. `period` is always a calendar-quarter
label like "2023Q4" so the commentary agent can join findings to the
quarter it is writing about.
"""

from functools import reduce
from pathlib import Path

import yaml
from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F
from pyspark.sql.window import Window

from src.pipeline.silver import calendar_quarter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_COLUMNS = ["rule", "cik", "metric", "period", "detail", "action"]


def load_anomaly_config() -> dict:
    with open(PROJECT_ROOT / "config" / "anomaly_config.yaml") as f:
        return yaml.safe_load(f)


def _quarter_label(year: Column, quarter: Column) -> Column:
    return F.concat(year.cast("string"), F.lit("Q"), quarter.cast("string"))


def _cq_period() -> Column:
    return _quarter_label(F.col("calendar_year"), F.col("calendar_quarter"))


def _ddate_period() -> Column:
    return _quarter_label(*calendar_quarter(F.col("ddate")))


def _log(df: DataFrame, rule: str, metric: Column, period: Column, detail: Column, action: str) -> DataFrame:
    return df.select(
        F.lit(rule).alias("rule"),
        F.col("cik"),
        metric.alias("metric"),
        period.alias("period"),
        detail.alias("detail"),
        F.lit(action).alias("action"),
    )


def check_gross_profit_identity(cq: DataFrame, tolerance: float) -> DataFrame:
    """gross_profit should equal revenue - cost_of_revenue within tolerance.
    A gross_profit that silver DERIVED that way passes trivially, so in
    practice this only fires on directly-reported values."""
    expected = F.col("revenue") - F.col("cost_of_revenue")
    rel_error = F.abs(F.col("gross_profit") - expected) / F.abs(expected)
    broken = cq.filter(
        F.col("revenue").isNotNull()
        & F.col("cost_of_revenue").isNotNull()
        & F.col("gross_profit").isNotNull()
        & (expected != 0)
        & (rel_error > tolerance)
    )
    return _log(
        broken,
        "gross_profit_identity",
        F.lit("gross_profit"),
        _cq_period(),
        F.format_string(
            "reported gross_profit=%.0f, revenue-cost_of_revenue=%.0f, relative error=%.4f",
            F.col("gross_profit"),
            expected,
            rel_error,
        ),
        "flagged",
    )


def check_balance_sheet_identity(cq: DataFrame, tolerance: float) -> DataFrame:
    """total_assets should equal total_liabilities + stockholders_equity.
    Filers with material non-controlling interest can legitimately break this
    when only the parent-only equity tag is reported — flagged, not fixed."""
    expected = F.col("total_liabilities") + F.col("stockholders_equity")
    rel_error = F.abs(F.col("total_assets") - expected) / F.abs(expected)
    broken = cq.filter(
        F.col("total_assets").isNotNull()
        & F.col("total_liabilities").isNotNull()
        & F.col("stockholders_equity").isNotNull()
        & (expected != 0)
        & (rel_error > tolerance)
    )
    return _log(
        broken,
        "balance_sheet_identity",
        F.lit("total_assets"),
        _cq_period(),
        F.format_string(
            "reported total_assets=%.0f, liabilities+equity=%.0f, relative error=%.4f",
            F.col("total_assets"),
            expected,
            rel_error,
        ),
        "flagged",
    )


def check_ytd_consistency(facts: DataFrame, tolerance: float) -> DataFrame:
    """The three reported quarters should sum to the nine-month YTD figure.
    This is what makes Q4 = annual - YTD9 trustworthy: if Q1+Q2+Q3 doesn't
    match YTD9, the derived Q4 inherits the error. Runs on per-filing facts
    (latest-filed value per period)."""
    ytd = facts.filter(F.col("qtrs") == 3).alias("y")
    quarters = facts.filter(F.col("qtrs") == 1).alias("q")
    # The three quarters inside a nine-month window end 0, ~91 and ~182 days
    # before the YTD period end; 200 days of slack covers 52/53-week years.
    joined = ytd.join(
        quarters,
        (F.col("y.cik") == F.col("q.cik"))
        & (F.col("y.metric") == F.col("q.metric"))
        & F.datediff(F.col("y.ddate"), F.col("q.ddate")).between(0, 200),
    )
    summed = joined.groupBy(
        F.col("y.cik").alias("cik"),
        F.col("y.metric").alias("metric"),
        F.col("y.ddate").alias("ddate"),
        F.col("y.value").alias("ytd_value"),
    ).agg(F.sum("q.value").alias("sum_of_quarters"), F.countDistinct("q.ddate").alias("n_quarters"))

    rel_error = F.abs(F.col("sum_of_quarters") - F.col("ytd_value")) / F.abs(F.col("ytd_value"))
    broken = summed.filter((F.col("n_quarters") == 3) & (F.col("ytd_value") != 0) & (rel_error > tolerance))
    return _log(
        broken,
        "ytd_consistency",
        F.col("metric"),
        _ddate_period(),
        F.format_string(
            "Q1+Q2+Q3=%.0f vs reported nine-month YTD=%.0f, relative error=%.4f",
            F.col("sum_of_quarters"),
            F.col("ytd_value"),
            rel_error,
        ),
        "flagged_q4_derivation_suspect",
    )


def check_scale_errors(cq: DataFrame, metric: str, multiple: float, min_history: int) -> DataFrame:
    """Flags a value that jumps by >= `multiple`x from the company's own
    trailing median for that metric — the signature of a thousands/millions
    scale mistake, not a real business change."""
    window_hist = Window.partitionBy("cik").orderBy("quarter_index").rowsBetween(-min_history, -1)
    with_median = cq.withColumn("trailing_median", F.percentile_approx(metric, 0.5).over(window_hist)).withColumn(
        "trailing_count", F.count(metric).over(window_hist)
    )

    ratio = F.abs(F.col(metric)) / F.abs(F.col("trailing_median"))
    flagged = with_median.filter(
        F.col(metric).isNotNull()
        & F.col("trailing_median").isNotNull()
        & (F.col("trailing_count") >= min_history)
        & (F.col("trailing_median") != 0)
        & ((ratio >= multiple) | (ratio <= 1 / multiple))
    )
    return _log(
        flagged,
        "scale_error",
        F.lit(metric),
        _cq_period(),
        F.format_string(
            "value=%.0f is %.4gx the trailing median of %.0f", F.col(metric), ratio, F.col("trailing_median")
        ),
        "flagged_not_auto_corrected",
    )


def check_sign_anomalies(cq: DataFrame) -> DataFrame:
    """Expenses and cost metrics are expected to be reported as non-negative
    magnitudes in this dataset's convention. A negative value is FLAGGED
    ONLY, never auto-corrected — some are legitimate (e.g. a R&D tax credit
    recorded as negative R&D expense in an unusual quarter)."""
    expense_metrics = ["cost_of_revenue", "research_and_development_expense", "sga_expense"]
    frames = [
        _log(
            cq.filter(F.col(metric) < 0),
            "sign_anomaly",
            F.lit(metric),
            _cq_period(),
            F.format_string("negative value: %.0f", F.col(metric)),
            "flagged_not_auto_corrected",
        )
        for metric in expense_metrics
    ]
    return reduce(DataFrame.unionByName, frames)


def check_restatements(facts: DataFrame, relative_threshold: float) -> DataFrame:
    """Same (cik, metric, ddate, qtrs) fact reported with a different value
    in a later filing = a restatement or recast. Needs the per-filing fact
    history (silver `build_facts`), not the de-duplicated panel. This rule
    only logs the event; both values stay in the fact history and the panel
    uses the latest — see docs/metric_dictionary.md."""
    key = ["cik", "metric", "ddate", "qtrs"]
    first_reported = (
        facts.withColumn("rn", F.row_number().over(Window.partitionBy(*key).orderBy("filed")))
        .filter(F.col("rn") == 1)
        .select(*key, F.col("value").alias("first_value"), F.col("filed").alias("first_filed"))
    )
    latest = (
        facts.withColumn("rn_desc", F.row_number().over(Window.partitionBy(*key).orderBy(F.col("filed").desc())))
        .filter(F.col("rn_desc") == 1)
        .select(*key, F.col("value").alias("latest_value"), F.col("filed").alias("latest_filed"))
    )

    rel_change = F.abs(F.col("latest_value") - F.col("first_value")) / F.abs(F.col("first_value"))
    material = first_reported.join(latest, on=key, how="inner").filter(
        (F.col("first_filed") != F.col("latest_filed"))
        & (F.col("first_value") != 0)
        & (rel_change > relative_threshold)
    )
    return _log(
        material,
        "restatement_or_recast",
        F.col("metric"),
        _ddate_period(),
        F.format_string(
            "qtrs=%d: first_reported=%.0f (filed %s) -> latest=%.0f (filed %s), relative change=%.4f",
            F.col("qtrs"),
            F.col("first_value"),
            F.col("first_filed").cast("string"),
            F.col("latest_value"),
            F.col("latest_filed").cast("string"),
            rel_change,
        ),
        "both_values_kept",
    )


def run_all_checks(facts: DataFrame, cq: DataFrame, cfg: dict | None = None) -> DataFrame:
    """facts: per-filing fact history (silver.build_facts). cq: the gold
    company-quarter table. Both are needed because identity/scale checks work
    on the pivoted table while restatement and YTD checks need filing-level
    history."""
    from src.pipeline.silver import latest_filed_facts

    cfg = cfg or load_anomaly_config()
    tol = cfg["identity_relative_tolerance"]
    checks = [
        check_gross_profit_identity(cq, tol),
        check_balance_sheet_identity(cq, tol),
        check_ytd_consistency(latest_filed_facts(facts), tol),
        check_sign_anomalies(cq),
        check_restatements(facts, cfg["restatement_relative_threshold"]),
    ]
    checks += [
        check_scale_errors(cq, metric, cfg["scale_error_multiple"], cfg["scale_error_min_history_quarters"])
        for metric in ["revenue", "operating_income", "net_income"]
    ]
    return reduce(DataFrame.unionByName, checks)


def write_anomaly_log(log: DataFrame, path: Path) -> None:
    log.write.mode("overwrite").format("parquet").save(str(path))


if __name__ == "__main__":
    from src.pipeline.gold import GOLD_DIR
    from src.pipeline.silver import SILVER_DIR
    from src.utils.spark_session import get_spark

    spark = get_spark("anomaly-check")
    facts = spark.read.parquet(str(SILVER_DIR / "facts_history"))
    cq = spark.read.parquet(str(GOLD_DIR / "company_quarter"))
    write_anomaly_log(run_all_checks(facts, cq), GOLD_DIR / "anomaly_log")
    log = spark.read.parquet(str(GOLD_DIR / "anomaly_log"))
    print(f"Anomaly log rows: {log.count():,}")
    log.groupBy("rule").count().show()
    spark.stop()
