"""
Silver layer: bronze facts -> one clean row per (company, metric, calendar
quarter), with Q4 derived, fiscal calendars aligned to calendar quarters, and
every value carrying the accession number(s) and XBRL tag it came from.

Key design choices (see docs/metric_dictionary.md for the reasoning):

1. Only consolidated, top-level, standard-taxonomy facts are kept: segments
   IS NULL, coreg IS NULL, and the tag's version is a us-gaap taxonomy (a
   company-extension tag can reuse a standard tag's name with a different
   meaning).

2. Tag de-duplication happens WITHIN a filing, not across filings. A filing
   that reports both `Revenues` and `RevenueFromContract...` for the same
   period keeps the preferred tag. The same period reported again in a later
   filing (a comparative column, or a restatement) is kept as its own row in
   `build_facts` so restatement detection can see both versions; the panel
   then uses the latest-filed value.

3. Calendar-quarter bucketing uses each fact's own period-end date shifted
   back 45 days (roughly the quarter's midpoint), not a hardcoded fiscal
   calendar. NVIDIA's quarter ending 2024-01-28 covers Nov-Jan, so it lands in
   calendar 2023 Q4; a 52/53-week filer whose year ends on Jan 1 lands in Q4,
   not in the next year's Q1.

4. Q4 is never directly reported. It's derived per (cik, metric) as the
   annual value (qtrs=4) minus the nine-month YTD value (qtrs=3) whose period
   ends ~one quarter earlier. Matching on period dates, not the filing's `fy`
   field, matters: a 10-K carries two or three years of comparative annual
   figures that all share one `fy`. If either side is missing, no Q4 row is
   produced rather than a guess.
"""

from pathlib import Path

import yaml
from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRONZE_DIR = PROJECT_ROOT / "data" / "bronze"
SILVER_DIR = PROJECT_ROOT / "data" / "silver"

ACCEPTED_FORMS = ["10-K", "10-Q", "10-K/A", "10-Q/A"]

# Days between a period's end date and its approximate midpoint. See point 3.
QUARTER_MIDPOINT_SHIFT_DAYS = 45

# A nine-month YTD period ends one fiscal quarter (~91 days) before the
# annual period it pairs with. The window allows for 52/53-week calendars.
Q4_GAP_MIN_DAYS, Q4_GAP_MAX_DAYS = 80, 100

PANEL_COLUMNS = [
    "cik",
    "name",
    "sic",
    "metric",
    "value",
    "ddate",
    "qtrs",
    "form",
    "fy",
    "fp",
    "filed",
    "calendar_year",
    "calendar_quarter",
    "is_derived",
    "derivation",
    "adsh",
    "tag",
]


def load_metric_dictionary() -> dict:
    with open(PROJECT_ROOT / "config" / "metric_dictionary.yaml") as f:
        return yaml.safe_load(f)


def _tag_to_metric_lookup(spark: SparkSession, metric_dict: dict) -> DataFrame:
    """Flattens the metric dictionary into (tag, metric, priority) rows for a join.
    Lower priority number = preferred tag when a filing reports more than one
    candidate tag for the same metric in the same period."""
    rows = []
    for metric, tags in metric_dict.items():
        if metric == "identities" or not isinstance(tags, list):
            continue
        for priority, tag in enumerate(tags):
            rows.append((tag, metric, priority))
    schema = T.StructType(
        [
            T.StructField("tag", T.StringType()),
            T.StructField("metric", T.StringType()),
            T.StructField("priority", T.IntegerType()),
        ]
    )
    return spark.createDataFrame(rows, schema)


def _read_bronze(spark: SparkSession, name: str, bronze_dir: Path | None = None) -> DataFrame:
    bronze_dir = bronze_dir or BRONZE_DIR
    fmt = "delta" if (bronze_dir / name / "_delta_log").exists() else "parquet"
    return spark.read.format(fmt).load(str(bronze_dir / name))


def select_preferred_tags(facts: DataFrame) -> DataFrame:
    """Within one filing, keep only the highest-priority tag per
    (metric, period). Different filings are never collapsed here."""
    window = Window.partitionBy("adsh", "cik", "metric", "ddate", "qtrs").orderBy("priority", "tag")
    return facts.withColumn("rn", F.row_number().over(window)).filter(F.col("rn") == 1).drop("rn")


def build_facts(
    spark: SparkSession, cik_allowlist: list[str] | None = None, bronze_dir: Path | None = None
) -> DataFrame:
    """Joins num+sub+tag_lookup into one row per (filing, metric, period)
    that matches a metric we track. The same period can appear in several
    filings; see `latest_filed_facts`. cik_allowlist restricts to specific
    companies; pass None to process every filer (needed for the sector
    benchmark)."""
    num = _read_bronze(spark, "num", bronze_dir)
    sub = _read_bronze(spark, "sub", bronze_dir)

    sub_f = sub.filter(F.col("form").isin(ACCEPTED_FORMS))
    if cik_allowlist:
        sub_f = sub_f.filter(F.col("cik").isin(cik_allowlist))

    tag_lookup = _tag_to_metric_lookup(spark, load_metric_dictionary())

    facts = (
        num.filter(
            F.col("segments").isNull()
            & F.col("coreg").isNull()
            & (F.col("uom") == "USD")
            & F.col("version").startswith("us-gaap")
        )
        .join(F.broadcast(tag_lookup), on="tag", how="inner")
        .join(
            sub_f.select("adsh", "cik", "name", "sic", "form", "fy", "fp", "period", "filed"),
            on="adsh",
            how="inner",
        )
        .withColumn("cik", F.lpad(F.col("cik"), 10, "0"))
        .withColumn("value", F.col("value").cast(T.DoubleType()))
        .withColumn("qtrs", F.col("qtrs").cast(T.IntegerType()))
        .withColumn("ddate", F.to_date("ddate", "yyyyMMdd"))
        .withColumn("filed", F.to_date("filed", "yyyyMMdd"))
        .filter(F.col("value").isNotNull())
    )
    return select_preferred_tags(facts)


def latest_filed_facts(facts: DataFrame) -> DataFrame:
    """One row per (cik, metric, ddate, qtrs): the most recently filed value,
    so the panel reflects restatements. The as-first-reported value is still
    visible in `build_facts` output and in the anomaly log."""
    window = Window.partitionBy("cik", "metric", "ddate", "qtrs").orderBy(F.col("filed").desc(), F.col("adsh").desc())
    return facts.withColumn("rn", F.row_number().over(window)).filter(F.col("rn") == 1).drop("rn")


def calendar_quarter(ddate_col: Column) -> tuple[Column, Column]:
    """Buckets a period-end date into (calendar_year, calendar_quarter) by the
    period's approximate midpoint — see module docstring, point 3."""
    midpoint = F.date_sub(ddate_col, QUARTER_MIDPOINT_SHIFT_DAYS)
    return F.year(midpoint), F.quarter(midpoint)


def _with_calendar_quarter(df: DataFrame, ddate_col: str = "ddate") -> DataFrame:
    year, quarter = calendar_quarter(F.col(ddate_col))
    return df.withColumn("calendar_year", year).withColumn("calendar_quarter", quarter)


def extract_quarterly_facts(facts: DataFrame) -> DataFrame:
    """Facts with qtrs == 1: a single fiscal quarter's duration."""
    return (
        _with_calendar_quarter(facts.filter(F.col("qtrs") == 1))
        .withColumn("is_derived", F.lit(False))
        .withColumn("derivation", F.lit("reported_quarterly"))
    )


def extract_instant_facts(facts: DataFrame) -> DataFrame:
    """Facts with qtrs == 0: balance-sheet snapshots, bucketed by the same
    rule so a year-end balance sheet lines up with that year's Q4."""
    return (
        _with_calendar_quarter(facts.filter(F.col("qtrs") == 0))
        .withColumn("is_derived", F.lit(False))
        .withColumn("derivation", F.lit("instant"))
    )


def derive_q4_facts(facts: DataFrame) -> DataFrame:
    """Q4 = annual (qtrs=4) - nine-month YTD (qtrs=3) for the same company and
    metric, where the YTD period ends ~one quarter before the annual period.
    Returns one row per annual fact that has a matching YTD fact; the result
    is a one-quarter duration (qtrs=1) marked is_derived=True, and its `adsh`
    cites both source filings."""
    annual = facts.filter(F.col("qtrs") == 4).alias("a")
    ytd9 = facts.filter(F.col("qtrs") == 3).alias("y")

    gap_days = F.datediff(F.col("a.ddate"), F.col("y.ddate"))
    matched = annual.join(
        ytd9,
        (F.col("a.cik") == F.col("y.cik"))
        & (F.col("a.metric") == F.col("y.metric"))
        & gap_days.between(Q4_GAP_MIN_DAYS, Q4_GAP_MAX_DAYS),
        how="inner",
    )
    year, quarter = calendar_quarter(F.col("a.ddate"))

    return matched.select(
        F.col("a.cik").alias("cik"),
        F.col("a.name").alias("name"),
        F.col("a.sic").alias("sic"),
        F.col("a.metric").alias("metric"),
        (F.col("a.value") - F.col("y.value")).alias("value"),
        F.col("a.ddate").alias("ddate"),
        F.lit(1).alias("qtrs"),
        F.col("a.form").alias("form"),
        F.col("a.fy").alias("fy"),
        F.lit("Q4").alias("fp"),
        F.col("a.filed").alias("filed"),
        year.alias("calendar_year"),
        quarter.alias("calendar_quarter"),
        F.lit(True).alias("is_derived"),
        F.lit("annual_minus_ytd9").alias("derivation"),
        F.concat_ws("|", F.col("a.adsh"), F.col("y.adsh")).alias("adsh"),
        F.concat_ws("|", F.col("a.tag"), F.col("y.tag")).alias("tag"),
    )


def add_derived_gross_profit(panel: DataFrame) -> DataFrame:
    """If a (cik, calendar_year, calendar_quarter) has revenue and
    cost_of_revenue but no directly-reported gross_profit, derive it and mark
    it so it's distinguishable from a disclosed value (see metric dictionary)."""
    key_cols = ["cik", "calendar_year", "calendar_quarter"]
    parts = panel.filter(F.col("metric").isin("revenue", "cost_of_revenue", "gross_profit"))
    pivoted = (
        parts.groupBy(*key_cols)
        .pivot("metric", ["revenue", "cost_of_revenue", "gross_profit"])
        .agg(F.first("value").alias("v"), F.first("adsh").alias("adsh"), F.first("tag").alias("tag"))
    )
    # Name/period columns are inherited from the revenue fact.
    revenue_meta = parts.filter(F.col("metric") == "revenue").drop(
        "metric", "value", "derivation", "is_derived", "adsh", "tag"
    )

    needs_derivation = (
        pivoted.filter(
            F.col("gross_profit_v").isNull() & F.col("revenue_v").isNotNull() & F.col("cost_of_revenue_v").isNotNull()
        )
        .select(
            *key_cols,
            (F.col("revenue_v") - F.col("cost_of_revenue_v")).alias("value"),
            F.concat_ws("|", "revenue_adsh", "cost_of_revenue_adsh").alias("adsh"),
            F.concat_ws("|", "revenue_tag", "cost_of_revenue_tag").alias("tag"),
        )
        .join(revenue_meta, on=key_cols, how="left")
        .withColumn("metric", F.lit("gross_profit"))
        .withColumn("is_derived", F.lit(True))
        .withColumn("derivation", F.lit("revenue_minus_cost_of_revenue"))
    )
    return panel.unionByName(needs_derivation, allowMissingColumns=True)


def resolve_one_value_per_quarter(panel: DataFrame) -> DataFrame:
    """Guarantees one row per (cik, metric, calendar quarter). A few filers
    tag a standalone Q4 (qtrs=1) in their 10-K as well; a reported value
    always beats a derived one, then the latest period end, then latest filing."""
    window = Window.partitionBy("cik", "metric", "calendar_year", "calendar_quarter").orderBy(
        F.col("is_derived").asc(), F.col("ddate").desc(), F.col("filed").desc()
    )
    return panel.withColumn("rn", F.row_number().over(window)).filter(F.col("rn") == 1).drop("rn")


def build_silver_panel_from_facts(facts: DataFrame) -> DataFrame:
    latest = latest_filed_facts(facts)
    panel = (
        extract_quarterly_facts(latest)
        .select(*PANEL_COLUMNS)
        .unionByName(extract_instant_facts(latest).select(*PANEL_COLUMNS))
        .unionByName(derive_q4_facts(latest).select(*PANEL_COLUMNS))
    )
    panel = resolve_one_value_per_quarter(panel)
    return add_derived_gross_profit(panel).select(*PANEL_COLUMNS)


def build_silver_panel(spark: SparkSession, cik_allowlist: list[str] | None = None) -> DataFrame:
    return build_silver_panel_from_facts(build_facts(spark, cik_allowlist))


def write_silver(panel: DataFrame, facts: DataFrame, fmt: str = "parquet", silver_dir: Path | None = None) -> None:
    silver_dir = silver_dir or SILVER_DIR
    # Static overwrite: a rebuild must drop quarters that no longer exist.
    (
        panel.write.mode("overwrite")
        .option("partitionOverwriteMode", "static")
        .partitionBy("calendar_year", "calendar_quarter")
        .format(fmt)
        .save(str(silver_dir / "facts_panel"))
    )
    # The full per-filing fact history, kept for restatement and YTD checks.
    facts.write.mode("overwrite").format(fmt).save(str(silver_dir / "facts_history"))


if __name__ == "__main__":
    from src.utils.spark_session import get_spark

    spark = get_spark("silver-build")
    facts = build_facts(spark)
    panel = build_silver_panel_from_facts(facts)
    write_silver(panel, facts)
    print(f"Silver panel rows: {spark.read.parquet(str(SILVER_DIR / 'facts_panel')).count():,}")
    spark.stop()
