"""
Gold layer: analysis-ready tables built from the silver facts panel.

Two outputs:
  - company_quarter: one row per (company, calendar quarter) with every
    tracked metric as a column, plus derived margins and QoQ/YoY changes.
  - sector_benchmark: quartiles of each ratio across every filer in the
    configured SIC code, per calendar quarter — this is what makes
    "Intel vs. sector" a real comparison instead of "Intel vs. nine
    hand-picked peers we already agree with."
"""

from pathlib import Path

import yaml
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SILVER_DIR = PROJECT_ROOT / "data" / "silver"
GOLD_DIR = PROJECT_ROOT / "data" / "gold"

METRIC_COLUMNS = [
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_and_development_expense",
    "sga_expense",
    "operating_income",
    "net_income",
    "total_assets",
    "total_liabilities",
    "stockholders_equity",
]

RATIO_COLUMNS = ["gross_margin_pct", "operating_margin_pct", "rd_intensity_pct", "net_margin_pct"]
CHANGE_METRICS = ["revenue", "gross_profit", "operating_income", "net_income"]


def _read_silver(spark: SparkSession) -> DataFrame:
    return spark.read.parquet(str(SILVER_DIR / "facts_panel"))


def load_sector_sic_code() -> str:
    with open(PROJECT_ROOT / "config" / "companies.yaml") as f:
        return str(yaml.safe_load(f)["sector_sic_code"])


def pivot_company_quarter(panel: DataFrame) -> DataFrame:
    """One row per (cik, calendar quarter). Name and SIC come from the
    company's most recent filing, so a mid-history rename doesn't split one
    company into two rows."""
    values = (
        panel.groupBy("cik", "calendar_year", "calendar_quarter").pivot("metric", METRIC_COLUMNS).agg(F.first("value"))
    )
    latest = Window.partitionBy("cik").orderBy(F.col("filed").desc())
    identity = panel.withColumn("rn", F.row_number().over(latest)).filter(F.col("rn") == 1).select("cik", "name", "sic")
    return values.join(identity, on="cik", how="left")


def add_ratios(cq: DataFrame) -> DataFrame:
    def ratio(numerator: str):
        return F.when(F.col("revenue") != 0, F.col(numerator) / F.col("revenue"))

    return (
        cq.withColumn("gross_margin_pct", ratio("gross_profit"))
        .withColumn("operating_margin_pct", ratio("operating_income"))
        .withColumn("rd_intensity_pct", ratio("research_and_development_expense"))
        .withColumn("net_margin_pct", ratio("net_income"))
    )


def add_period_over_period_changes(cq: DataFrame) -> DataFrame:
    """QoQ compares to the immediately preceding CALENDAR quarter; YoY to the
    same calendar quarter one year earlier. Both are explicit joins on a
    contiguous quarter index, not row offsets, so a quarter missing from the
    data yields a null change instead of silently comparing the wrong periods."""
    # Contiguous index: 2023Q4 -> 8095, 2024Q1 -> 8096.
    cq = cq.withColumn("quarter_index", F.col("calendar_year") * 4 + F.col("calendar_quarter") - 1)

    def prior(offset: int, suffix: str) -> DataFrame:
        return cq.select(
            "cik",
            (F.col("quarter_index") + offset).alias("quarter_index"),
            *[F.col(m).alias(f"{m}_{suffix}") for m in CHANGE_METRICS],
        )

    out = cq.join(prior(1, "prior_q"), on=["cik", "quarter_index"], how="left").join(
        prior(4, "prior_y"), on=["cik", "quarter_index"], how="left"
    )

    def pct_change(metric: str, base: str):
        return F.when(F.col(base).isNotNull() & (F.col(base) != 0), (F.col(metric) - F.col(base)) / F.abs(F.col(base)))

    for m in CHANGE_METRICS:
        out = out.withColumn(f"{m}_qoq_pct", pct_change(m, f"{m}_prior_q")).withColumn(
            f"{m}_yoy_pct", pct_change(m, f"{m}_prior_y")
        )
    return out.drop(*[f"{m}_{s}" for m in CHANGE_METRICS for s in ("prior_q", "prior_y")])


def build_company_quarter_table_from_panel(panel: DataFrame) -> DataFrame:
    return add_period_over_period_changes(add_ratios(pivot_company_quarter(panel)))


def build_company_quarter_table(spark: SparkSession) -> DataFrame:
    return build_company_quarter_table_from_panel(_read_silver(spark))


def build_sector_benchmark(cq: DataFrame, sic_code: str | None) -> DataFrame:
    """25th/50th/75th percentile of each ratio per calendar quarter, across
    every filer with the given SIC code (pass None to benchmark everything
    in `cq`). Only filers that reported revenue that quarter count."""
    sector = cq.filter(F.col("revenue").isNotNull() & (F.col("revenue") > 0))
    if sic_code is not None:
        sector = sector.filter(F.col("sic") == sic_code)

    agg_exprs = []
    for c in RATIO_COLUMNS:
        agg_exprs += [
            F.percentile_approx(c, 0.25).alias(f"{c}_p25"),
            F.percentile_approx(c, 0.5).alias(f"{c}_median"),
            F.percentile_approx(c, 0.75).alias(f"{c}_p75"),
        ]
    agg_exprs.append(F.countDistinct("cik").alias("filer_count"))
    return sector.groupBy("calendar_year", "calendar_quarter").agg(*agg_exprs)


def write_gold(cq: DataFrame, benchmark: DataFrame, fmt: str = "parquet", gold_dir: Path | None = None) -> None:
    gold_dir = gold_dir or GOLD_DIR
    cq.write.mode("overwrite").format(fmt).save(str(gold_dir / "company_quarter"))
    benchmark.write.mode("overwrite").format(fmt).save(str(gold_dir / "sector_benchmark"))


def export_for_tableau(cq: DataFrame, benchmark: DataFrame, gold_dir: Path | None = None) -> None:
    """Tableau Public reads CSV more easily than partitioned Parquet directories."""
    export_dir = (gold_dir or GOLD_DIR) / "tableau_export"
    export_dir.mkdir(parents=True, exist_ok=True)
    cq.toPandas().to_csv(export_dir / "company_quarter.csv", index=False)
    benchmark.toPandas().to_csv(export_dir / "sector_benchmark.csv", index=False)


if __name__ == "__main__":
    from src.utils.spark_session import get_spark

    spark = get_spark("gold-build")
    cq = build_company_quarter_table(spark)
    benchmark = build_sector_benchmark(cq, load_sector_sic_code())
    write_gold(cq, benchmark)
    cq = spark.read.parquet(str(GOLD_DIR / "company_quarter"))
    benchmark = spark.read.parquet(str(GOLD_DIR / "sector_benchmark"))
    export_for_tableau(cq, benchmark)
    print(f"Gold company_quarter rows: {cq.count():,}")
    spark.stop()
