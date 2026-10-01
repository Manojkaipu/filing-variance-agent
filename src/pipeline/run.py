"""
Runs bronze -> silver -> gold -> anomaly log in one Spark session against a
data directory laid out as <data_dir>/{raw,bronze,silver,gold}.

    python -m src.pipeline.run                 # data/
    python -m src.pipeline.run --data-dir data/demo

Each stage reads the previous stage's output back from disk rather than
chaining lazy DataFrames, so a failure points at one stage and each layer is
inspectable on its own.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from src.anomaly.rules import run_all_checks, write_anomaly_log
from src.pipeline.bronze import ingest_all_available_quarters
from src.pipeline.gold import (
    build_company_quarter_table_from_panel,
    build_sector_benchmark,
    export_for_tableau,
    load_sector_sic_code,
    write_gold,
)
from src.pipeline.silver import build_facts, build_silver_panel_from_facts, write_silver

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_pipeline(spark, data_dir: Path, sic_code: str | None = None, tableau: bool = True) -> dict:
    raw, bronze, silver, gold = (data_dir / d for d in ("raw", "bronze", "silver", "gold"))
    started = time.perf_counter()

    def stage(name: str):
        print(f"== {name} ({time.perf_counter() - started:.0f}s elapsed) ==", flush=True)

    stage("bronze")
    ingest_all_available_quarters(spark, raw_dir=raw, bronze_dir=bronze)

    stage("silver")
    facts = build_facts(spark, bronze_dir=bronze)
    write_silver(build_silver_panel_from_facts(facts), facts, silver_dir=silver)

    stage("gold")
    panel = spark.read.parquet(str(silver / "facts_panel"))
    cq = build_company_quarter_table_from_panel(panel)
    write_gold(cq, build_sector_benchmark(cq, sic_code), gold_dir=gold)

    stage("anomaly")
    cq = spark.read.parquet(str(gold / "company_quarter"))
    write_anomaly_log(run_all_checks(spark.read.parquet(str(silver / "facts_history")), cq), gold / "anomaly_log")

    if tableau:
        stage("tableau export")
        export_for_tableau(cq, spark.read.parquet(str(gold / "sector_benchmark")), gold_dir=gold)

    log = spark.read.parquet(str(gold / "anomaly_log"))
    summary = {
        "silver_panel_rows": panel.count(),
        "company_quarters": cq.count(),
        "companies": cq.select("cik").distinct().count(),
        "anomalies": {r["rule"]: r["count"] for r in log.groupBy("rule").count().collect()},
    }
    print(f"Pipeline complete: {summary}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--no-tableau", action="store_true")
    parser.add_argument("--driver-memory", help="e.g. 5g — needed for a full-filer run; see src/utils/spark_session.py")
    parser.add_argument("--shuffle-partitions", type=int, default=8)
    args = parser.parse_args()

    from src.utils.spark_session import get_spark

    spark = get_spark(
        "filing-variance-pipeline",
        shuffle_partitions=args.shuffle_partitions,
        driver_memory=args.driver_memory,
    )
    try:
        run_pipeline(spark, args.data_dir.resolve(), sic_code=load_sector_sic_code(), tableau=not args.no_tableau)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
