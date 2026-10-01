"""
Shared Spark session builder. All pipeline stages (bronze, silver, gold) and
the test suite call get_spark() instead of building their own SparkSession,
so configuration lives in exactly one place.

Delta Lake is OPT-IN (use_delta=True), not default. The first time a Spark
session configures Delta, Spark's launcher resolves the delta-spark Maven
coordinate over the network (repo1.maven.org) before the JVM even starts —
if that network path is blocked (a locked-down sandbox, a corporate proxy,
an offline machine) the whole session fails to start, not just the Delta
write. Plain Parquet needs no such resolution and works everywhere. Turn on
Delta once you're running this somewhere with normal internet access and
want the time-travel/audit-history benefit.
"""

import os
import sys

from pyspark.sql import SparkSession


def get_spark(
    app_name: str = "filing-variance-agent",
    use_delta: bool = False,
    master: str | None = None,
    shuffle_partitions: int = 8,
    driver_memory: str | None = None,
) -> SparkSession:
    # Spark launches Python workers as `python3` by default, which doesn't
    # exist on Windows and may be a different interpreter elsewhere. Pin the
    # workers to the interpreter running the driver.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    # In local mode the driver IS the executor, and its heap is fixed when the
    # JVM starts — later .config("spark.driver.memory") is silently ignored.
    # It has to go through the submit args, before the gateway launches.
    # Default 1g is not enough for a full-filer run (~70M facts); set
    # FVA_DRIVER_MEMORY=6g or pass driver_memory for those.
    driver_memory = driver_memory or os.environ.get("FVA_DRIVER_MEMORY")
    if driver_memory:
        os.environ["PYSPARK_SUBMIT_ARGS"] = f"--driver-memory {driver_memory} pyspark-shell"

    builder = (
        SparkSession.builder.appName(app_name)
        # Local/dev sizing. Raise this when running against the full dataset
        # (2018-2025, ~30 quarters, tens of millions of facts) on a bigger
        # machine — the default 200 is tuned for clusters, not a laptop.
        .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
        # Overwriting a partitioned table replaces only the partitions being
        # written. Without this, ingesting quarter N deletes quarters 1..N-1.
        .config("spark.sql.sources.partitionOverwriteMode", "dynamic")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.showConsoleProgress", "false")
    )
    if master:
        builder = builder.master(master)

    if use_delta:
        builder = builder.config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension").config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        from delta import configure_spark_with_delta_pip

        builder = configure_spark_with_delta_pip(builder)

    return builder.getOrCreate()
