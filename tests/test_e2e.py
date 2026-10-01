"""
End to end on synthetic SEC filings: raw files -> bronze -> silver -> gold ->
anomaly log -> fact sheet -> draft -> verification against the raw filing
rows -> review queue -> exported commentary. No network, no API key.
"""

import dataclasses

import pytest
from pyspark.sql import functions as F

from src.agent.drafter import Sentence, TemplateDrafter
from src.agent.facts import build_fact_sheet
from src.agent.verifier import spark_ledger, verify
from src.demo.synthetic_filings import ticker_map, write_raw
from src.pipeline.run import run_pipeline
from src.review.queue import APPROVED, BLOCKED, ReviewQueue

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def demo(spark, spark_writes, tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("demo")
    write_raw(data_dir / "raw")
    summary = run_pipeline(spark, data_dir, sic_code="3674", tableau=False)
    return data_dir, summary


def test_pipeline_outputs(spark, demo):
    data_dir, summary = demo
    assert summary["companies"] == 5

    # Every filing quarter survives bronze ingestion.
    quarters = spark.read.parquet(str(data_dir / "bronze" / "num")).select("source_quarter").distinct().count()
    assert quarters == len(list((data_dir / "raw").iterdir()))

    cq = spark.read.parquet(str(data_dir / "gold" / "company_quarter"))
    # The 53-week year ending 2025-01-04 is calendar 2024Q4, not 2025Q1.
    gamma = {
        (r["calendar_year"], r["calendar_quarter"]) for r in cq.filter(F.col("cik") == ticker_map()["GAMMA"]).collect()
    }
    assert (2024, 4) in gamma and (2025, 1) not in gamma

    bench = spark.read.parquet(str(data_dir / "gold" / "sector_benchmark")).collect()
    assert {r["filer_count"] for r in bench} == {4}  # the retailer is excluded

    log = spark.read.parquet(str(data_dir / "gold" / "anomaly_log"))
    found = {(r["rule"], r["cik"], r["period"]) for r in log.select("rule", "cik", "period").collect()}
    assert ("restatement_or_recast", ticker_map()["DEMO"], "2023Q4") in found
    assert ("scale_error", ticker_map()["DELTA"], "2024Q2") in found
    assert ("ytd_consistency", ticker_map()["DELTA"], "2024Q3") in found


def test_restated_quarter_uses_latest_filed_value(spark, demo):
    data_dir, _ = demo
    panel = spark.read.parquet(str(data_dir / "silver" / "facts_panel"))
    row = panel.filter(
        (F.col("cik") == ticker_map()["DEMO"])
        & (F.col("metric") == "revenue")
        & (F.col("calendar_year") == 2023)
        & (F.col("calendar_quarter") == 4)
    ).collect()[0]
    assert row["derivation"] == "annual_minus_ytd9"
    # Q4 derived from the FY2024 10-K's restated FY2023 comparative.
    assert row["adsh"].split("|")[0].endswith("-25-000024")


@pytest.fixture(scope="module")
def sheet_and_ledger(spark, demo):
    data_dir, _ = demo
    tickers = ticker_map()
    peers = {t: tickers[t] for t in ("ALPHA", "BETA", "GAMMA")}
    sheet = build_fact_sheet(
        spark,
        "DEMO",
        tickers["DEMO"],
        "2024Q4",
        peers=peers,
        sector_sic_code="3674",
        data_dir=data_dir,
        company_name="Demo Silicon Corp",
    )
    return sheet, spark_ledger(spark, sheet, data_dir / "bronze")


def test_draft_verifies_against_raw_filings_and_flows_through_review(sheet_and_ledger, tmp_path):
    sheet, ledger = sheet_and_ledger
    assert sheet.get("DEMO.revenue.2024Q4").source.is_derived  # fourth quarter -> derived
    assert any(a["rule"] == "restatement_or_recast" for a in sheet.anomalies)

    draft = TemplateDrafter().draft(sheet)
    report = verify(draft, sheet, ledger)
    assert report.passed, [i for i in report.issues if i.level == "error"]
    assert report.facts_source_checked >= 5

    queue = ReviewQueue(tmp_path / "q.db")
    draft_id = queue.submit(sheet, draft, report)
    queue.approve(draft_id, reviewer="Test Reviewer")
    assert queue.get(draft_id).status == APPROVED
    md = queue.export_markdown(draft_id)
    assert "Demo Silicon Corp (DEMO)" in md and "sec.gov/Archives/edgar/data/900001/" in md


def test_tampered_draft_is_blocked(sheet_and_ledger, tmp_path):
    sheet, ledger = sheet_and_ledger
    draft = TemplateDrafter().draft(sheet)
    headline = draft.sentences[0]
    rev = sheet.get("DEMO.revenue.2024Q4").value
    tampered = f"Demo Silicon Corp reported 2024Q4 revenue of ${rev * 1.02 / 1e9:.2f} billion."
    draft.sentences[0] = dataclasses.replace(headline, text=tampered)
    draft.sentences.append(Sentence("revenue", "Non-GAAP revenue grew.", []))

    report = verify(draft, sheet, ledger)
    assert not report.passed
    assert {"number_mismatch", "non_gaap"} <= {i.code for i in report.issues}
    queue = ReviewQueue(tmp_path / "q.db")
    assert queue.get(queue.submit(sheet, draft, report)).status == BLOCKED
