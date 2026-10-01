"""
Bronze layer: convert the SEC's raw tab-separated quarterly files to Parquet.
No filtering, no joins, no cleaning — bronze is a faithful, queryable copy of
exactly what the SEC published. Every later bug gets debugged by comparing
against bronze, so it must never be "improved."

Input layout expected (produced by src/download/edgar_downloader.py):
    data/raw/<quarter>/sub.txt
    data/raw/<quarter>/num.txt
    data/raw/<quarter>/tag.txt

Output:
    data/bronze/sub/quarter=<quarter>/...
    data/bronze/num/quarter=<quarter>/...
    data/bronze/tag/quarter=<quarter>/...
"""

from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
BRONZE_DIR = PROJECT_ROOT / "data" / "bronze"

# Schemas are declared explicitly rather than inferred. Inferring a schema
# over 30 quarters of files is slow (Spark has to scan everything twice) and
# risky (a single quarter with an all-null column can make inference guess
# the wrong type for every other quarter). These match the SEC's documented
# layout for num.txt, sub.txt, and tag.txt.

SUB_SCHEMA = T.StructType(
    [
        T.StructField("adsh", T.StringType()),  # accession number — filing ID
        T.StructField("cik", T.StringType()),
        T.StructField("name", T.StringType()),
        T.StructField("sic", T.StringType()),
        T.StructField("countryba", T.StringType()),
        T.StructField("stprba", T.StringType()),
        T.StructField("cityba", T.StringType()),
        T.StructField("zipba", T.StringType()),
        T.StructField("bas1", T.StringType()),
        T.StructField("bas2", T.StringType()),
        T.StructField("baph", T.StringType()),
        T.StructField("countryma", T.StringType()),
        T.StructField("stprma", T.StringType()),
        T.StructField("cityma", T.StringType()),
        T.StructField("zipma", T.StringType()),
        T.StructField("mas1", T.StringType()),
        T.StructField("mas2", T.StringType()),
        T.StructField("countryinc", T.StringType()),
        T.StructField("stprinc", T.StringType()),
        T.StructField("ein", T.StringType()),
        T.StructField("former", T.StringType()),
        T.StructField("changed", T.StringType()),
        T.StructField("afs", T.StringType()),
        T.StructField("wksi", T.StringType()),
        T.StructField("fye", T.StringType()),  # fiscal year end, MMDD
        T.StructField("form", T.StringType()),  # 10-K, 10-Q, 10-K/A, ...
        T.StructField("period", T.StringType()),  # YYYYMMDD
        T.StructField("fy", T.StringType()),
        T.StructField("fp", T.StringType()),  # Q1, Q2, Q3, FY
        T.StructField("filed", T.StringType()),  # YYYYMMDD
        T.StructField("accepted", T.StringType()),
        T.StructField("prevrpt", T.StringType()),
        T.StructField("detail", T.StringType()),
        T.StructField("instance", T.StringType()),
        T.StructField("nciks", T.StringType()),
        T.StructField("aciks", T.StringType()),
    ]
)

NUM_SCHEMA = T.StructType(
    [
        T.StructField("adsh", T.StringType()),
        T.StructField("tag", T.StringType()),
        T.StructField("version", T.StringType()),
        T.StructField("ddate", T.StringType()),  # period end date, YYYYMMDD
        T.StructField("qtrs", T.StringType()),  # duration in quarters, 0/1/2/3/4
        T.StructField("uom", T.StringType()),  # unit of measure, e.g. USD
        T.StructField("segments", T.StringType()),
        T.StructField("coreg", T.StringType()),
        T.StructField("value", T.StringType()),
        T.StructField("footnote", T.StringType()),
    ]
)

TAG_SCHEMA = T.StructType(
    [
        T.StructField("tag", T.StringType()),
        T.StructField("version", T.StringType()),
        T.StructField("custom", T.StringType()),
        T.StructField("abstract", T.StringType()),
        T.StructField("datatype", T.StringType()),
        T.StructField("iord", T.StringType()),  # I = instant, D = duration
        T.StructField("crdr", T.StringType()),  # credit/debit
        T.StructField("tlabel", T.StringType()),
        T.StructField("doc", T.StringType()),
    ]
)


def _read_quarter_file(spark: SparkSession, quarter_dir: Path, filename: str, schema: T.StructType) -> DataFrame:
    """Reads by header name, not by position. The SEC has changed column
    order and added columns across releases (num.txt gained `segments`), so
    a positional schema silently shifts values into the wrong columns on
    older quarters. Every column is read as a string (no inference) and
    projected onto the declared schema; a column absent from this release's
    file comes through as null."""
    raw = (
        spark.read.option("header", True)
        .option("sep", "\t")
        .option("quote", "")  # sub.txt free-text fields contain stray quote characters
        .option("encoding", "UTF-8")
        .csv(str(quarter_dir / filename))
    )
    present = set(raw.columns)
    return raw.select(
        *[(F.col(f.name) if f.name in present else F.lit(None)).cast(f.dataType).alias(f.name) for f in schema.fields]
    ).withColumn("source_quarter", F.lit(quarter_dir.name))


def ingest_quarter(
    spark: SparkSession, quarter: str, fmt: str = "parquet", raw_dir: Path = RAW_DIR, bronze_dir: Path = BRONZE_DIR
) -> dict:
    """Reads one quarter's sub/num/tag files and writes each to bronze,
    partitioned by source_quarter. Only this quarter's partition is replaced
    (dynamic partition overwrite), so ingesting quarters one at a time never
    deletes the ones already loaded. Returns row counts for logging.
    fmt: "parquet" (default, no extra setup) or "delta" (pass use_delta=True
    to get_spark() first — see src/utils/spark_session.py)."""
    quarter_dir = raw_dir / quarter
    if not quarter_dir.exists():
        raise FileNotFoundError(f"{quarter_dir} not found. Run src/download/edgar_downloader.py first.")

    counts = {}
    for name, schema in (("sub", SUB_SCHEMA), ("num", NUM_SCHEMA), ("tag", TAG_SCHEMA)):
        df = _read_quarter_file(spark, quarter_dir, f"{name}.txt", schema)
        writer = df.write.mode("overwrite").partitionBy("source_quarter")
        if fmt == "delta":
            writer = writer.option("replaceWhere", f"source_quarter = '{quarter}'")
        else:
            writer = writer.option("partitionOverwriteMode", "dynamic")
        writer.format(fmt).save(str(bronze_dir / name))
        counts[name] = df.count()
    return counts


def ingest_all_available_quarters(
    spark: SparkSession, fmt: str = "parquet", raw_dir: Path = RAW_DIR, bronze_dir: Path = BRONZE_DIR
) -> dict:
    """Ingests every quarter directory found under data/raw/."""
    all_counts = {}
    for quarter_dir in sorted(raw_dir.iterdir()):
        if quarter_dir.is_dir() and (quarter_dir / "num.txt").exists():
            print(f"Ingesting {quarter_dir.name} into bronze...")
            all_counts[quarter_dir.name] = ingest_quarter(
                spark, quarter_dir.name, fmt=fmt, raw_dir=raw_dir, bronze_dir=bronze_dir
            )
    if not all_counts:
        raise FileNotFoundError(f"No quarter directories with num.txt under {raw_dir}.")
    total_rows = sum(c["num"] for c in all_counts.values())
    print(f"Bronze ingestion complete. Total num.txt rows: {total_rows:,}")
    return all_counts


if __name__ == "__main__":
    from src.utils.spark_session import get_spark

    spark = get_spark("bronze-ingest")
    ingest_all_available_quarters(spark)
    spark.stop()
