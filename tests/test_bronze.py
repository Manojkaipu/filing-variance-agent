from src.pipeline.bronze import ingest_all_available_quarters


def write_quarter(raw_dir, quarter, num_header, num_rows):
    qdir = raw_dir / quarter
    qdir.mkdir(parents=True)
    (qdir / "sub.txt").write_text("adsh\tcik\tname\tform\n" + f"A-{quarter}\t1\tACME\t10-Q\n")
    (qdir / "tag.txt").write_text("tag\tversion\nRevenues\tus-gaap/2023\n")
    (qdir / "num.txt").write_text("\t".join(num_header) + "\n" + "".join("\t".join(r) + "\n" for r in num_rows))


def test_columns_are_read_by_name_across_layout_changes(spark, spark_writes, tmp_path):
    raw, bronze = tmp_path / "raw", tmp_path / "bronze"
    # Older layout: no `segments` column, coreg before ddate.
    write_quarter(
        raw,
        "2023q1",
        ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "value", "footnote"],
        [["A-2023q1", "Revenues", "us-gaap/2023", "", "20230331", "1", "USD", "1000", ""]],
    )
    # Newer layout with `segments`.
    write_quarter(
        raw,
        "2024q1",
        ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value", "footnote"],
        [
            ["A-2024q1", "Revenues", "us-gaap/2023", "20240331", "1", "USD", "", "", "2000", ""],
            ["A-2024q1", "Revenues", "us-gaap/2023", "20240331", "1", "USD", "Segment=X;", "", "5", ""],
        ],
    )

    counts = ingest_all_available_quarters(spark, raw_dir=raw, bronze_dir=bronze)
    assert counts["2023q1"]["num"] == 1 and counts["2024q1"]["num"] == 2

    num = spark.read.parquet(str(bronze / "num"))
    rows = {(r["source_quarter"], r["value"], r["segments"]) for r in num.collect()}
    # Values land in `value` for both layouts (a positional schema would put
    # "20230331" in qtrs and "1000" in segments for the old one).
    assert rows == {("2023q1", "1000", None), ("2024q1", "2000", None), ("2024q1", "5", "Segment=X;")}
    assert {r["ddate"] for r in num.collect()} == {"20230331", "20240331"}


def test_reingesting_one_quarter_keeps_the_others(spark, spark_writes, tmp_path):
    raw, bronze = tmp_path / "raw", tmp_path / "bronze"
    header = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value", "footnote"]
    for q in ("2023q1", "2023q2", "2023q3"):
        write_quarter(raw, q, header, [[f"A-{q}", "Revenues", "us-gaap/2023", "20230331", "1", "USD", "", "", "1", ""]])

    ingest_all_available_quarters(spark, raw_dir=raw, bronze_dir=bronze)
    quarters = {r[0] for r in spark.read.parquet(str(bronze / "num")).select("source_quarter").collect()}
    # Static partition overwrite would have left only the last quarter written.
    assert quarters == {"2023q1", "2023q2", "2023q3"}
