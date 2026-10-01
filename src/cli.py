"""
Command-line entry point.

    python -m src.cli demo                                   # synthetic filings -> pipeline -> draft -> queue
    python -m src.cli draft --ticker INTC --quarter 2024Q2   # Claude drafts, verifier checks, queue holds it
    python -m src.cli draft --ticker INTC --quarter 2024Q2 --offline   # template drafter, no API key
    python -m src.cli queue [--status pending_review]
    python -m src.cli show 3
    python -m src.cli approve 3 --reviewer "A. Analyst" [--note ...] [--override]
    python -m src.cli reject 3 --reviewer "A. Analyst" --note "..."
    python -m src.cli export 3 [--out commentary.md]
    python -m src.cli eval verifier --ticker INTC --quarter 2024Q2   # recall/specificity, free
    python -m src.cli eval drafter --quarters 2024Q1,2024Q2          # pass rate (costs API budget)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from src.review.queue import DEFAULT_DB, ReviewError, ReviewQueue

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEMO_DATA_DIR = PROJECT_ROOT / "data" / "demo"


def resolve_universe(data_dir: Path) -> tuple[dict[str, str], dict[str, str], str | None]:
    """({ticker: cik}, {ticker: display name}, sector SIC code) for the focal
    company and peers. A data dir may carry its own universe.json (the demo
    does); otherwise the universe comes from config/companies.yaml resolved
    against the SEC's ticker file."""
    override = data_dir / "universe.json"
    if override.exists():
        data = json.loads(override.read_text())
        return data["tickers"], data.get("names", {}), data.get("sector_sic_code")
    from src.download.edgar_downloader import resolve_company_ciks

    with open(PROJECT_ROOT / "config" / "companies.yaml") as f:
        cfg = yaml.safe_load(f)
    names = {c["ticker"]: c["name"] for c in [cfg["focal_company"], *cfg["peers"]]}
    return resolve_company_ciks(), names, str(cfg["sector_sic_code"])


def draft_and_submit(
    spark, *, ticker: str, period: str, data_dir: Path, db: Path, offline: bool, request: str | None, model: str | None
) -> int:
    from src.agent.drafter import DEFAULT_MODEL, get_drafter
    from src.agent.facts import build_fact_sheet
    from src.agent.verifier import spark_ledger, verify

    universe, names, sic = resolve_universe(data_dir)
    ticker = ticker.upper()
    if ticker not in universe:
        raise SystemExit(f"{ticker} is not in the configured universe: {sorted(universe)}")
    peers = {t: c for t, c in universe.items() if t != ticker}

    sheet = build_fact_sheet(
        spark,
        ticker,
        universe[ticker],
        period,
        peers=peers,
        sector_sic_code=sic,
        data_dir=data_dir,
        company_name=names.get(ticker),
    )
    print(
        f"Fact sheet: {len(sheet.facts)} facts, {len(sheet.anomalies)} anomaly-log entries for "
        f"{sheet.company_name} {sheet.period}"
    )

    draft = get_drafter(offline, model or DEFAULT_MODEL).draft(sheet, request)
    print(f"Draft: {len(draft.sentences)} sentences from {draft.drafter}")

    report = verify(draft, sheet, spark_ledger(spark, sheet, data_dir / "bronze"))
    print(f"Verification {report.summary()}")

    draft_id = ReviewQueue(db).submit(sheet, draft, report)
    status = "pending_review" if report.passed else "blocked (verification failed)"
    print(f"Queued as draft #{draft_id}: {status}. Next: python -m src.cli show {draft_id}")
    return draft_id


def cmd_draft(args) -> None:
    from src.utils.spark_session import get_spark

    spark = get_spark("commentary-draft")
    spark.sparkContext.setLogLevel("ERROR")
    try:
        draft_and_submit(
            spark,
            ticker=args.ticker,
            period=args.quarter,
            data_dir=args.data_dir.resolve(),
            db=args.db,
            offline=args.offline,
            request=args.request,
            model=args.model,
        )
    finally:
        spark.stop()


def cmd_demo(args) -> None:
    import shutil

    from src.demo.synthetic_filings import COMPANIES, ticker_map, write_raw
    from src.pipeline.run import run_pipeline
    from src.utils.spark_session import get_spark

    data_dir = DEMO_DATA_DIR
    if data_dir.exists():
        shutil.rmtree(data_dir)
    quarters = write_raw(data_dir / "raw")
    semis = [c for c in COMPANIES if c.sic == "3674"]
    (data_dir / "universe.json").write_text(
        json.dumps(
            {
                "tickers": {c.ticker: ticker_map()[c.ticker] for c in semis},
                "names": {c.ticker: c.name for c in semis},
                "sector_sic_code": "3674",
            },
            indent=2,
        )
    )
    print(f"Wrote synthetic filings for {len(COMPANIES)} fictional companies across {len(quarters)} quarters.")

    spark = get_spark("filing-variance-demo")
    spark.sparkContext.setLogLevel("ERROR")
    try:
        run_pipeline(spark, data_dir, sic_code="3674")
        draft_and_submit(
            spark,
            ticker="DEMO",
            period=args.quarter,
            data_dir=data_dir,
            db=args.db,
            offline=not args.online,
            request=None,
            model=None,
        )
    finally:
        spark.stop()


def _load_case(spark, *, ticker: str, period: str, data_dir: Path, with_peers: bool = True):
    """(fact sheet, ledger) for one company-quarter, or None if the pipeline
    has no data for it."""
    from src.agent.facts import build_fact_sheet
    from src.agent.verifier import spark_ledger

    universe, names, sic = resolve_universe(data_dir)
    peers = {t: c for t, c in universe.items() if t != ticker} if with_peers else {}
    try:
        sheet = build_fact_sheet(
            spark, ticker, universe[ticker], period, peers=peers, sector_sic_code=sic,
            data_dir=data_dir, company_name=names.get(ticker),
        )
    except LookupError as e:
        print(f"  skipping {ticker} {period}: {e}")
        return None
    return sheet, spark_ledger(spark, sheet, data_dir / "bronze")


def cmd_eval(args) -> None:
    from src.eval.harness import (
        format_drafter_report,
        format_verifier_report,
        run_drafter_eval,
        run_verifier_eval,
        write_report,
    )
    from src.utils.spark_session import get_spark

    data_dir = args.data_dir.resolve()
    spark = get_spark("commentary-eval", driver_memory=args.driver_memory)
    spark.sparkContext.setLogLevel("ERROR")
    try:
        if args.mode == "verifier":
            from src.agent.drafter import TemplateDrafter

            case = _load_case(spark, ticker=args.ticker.upper(), period=args.quarter, data_dir=data_dir)
            if case is None:
                raise SystemExit("No data for that company-quarter; run the pipeline first.")
            sheet, ledger = case
            # The baseline draft has to pass on its own merits before any
            # injected defect tells us something, so it comes from the
            # deterministic drafter rather than a model.
            result = run_verifier_eval(TemplateDrafter().draft(sheet), sheet, ledger)
            result["case"] = f"{sheet.ticker} {sheet.period}"
            print(format_verifier_report(result))
        else:
            from src.agent.drafter import DEFAULT_MODEL, get_drafter

            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            quarters = [q.strip() for q in args.quarters.split(",") if q.strip()]
            cases = []
            for ticker in tickers:
                for quarter in quarters:
                    case = _load_case(spark, ticker=ticker, period=quarter, data_dir=data_dir)
                    if case is not None:
                        cases.append(case)
            if not cases:
                raise SystemExit("No usable company-quarters; run the pipeline first.")

            drafter = get_drafter(args.offline, args.model or DEFAULT_MODEL)
            print(f"Drafting {len(cases)} company-quarters with {'template' if args.offline else args.model or DEFAULT_MODEL}...")
            result = run_drafter_eval(drafter, cases)
            print(format_drafter_report(result))

        if args.out:
            write_report(result, args.out)
            print(f"\nWrote {args.out}")
    finally:
        spark.stop()


def cmd_queue(args) -> None:
    rows = ReviewQueue(args.db).list(args.status)
    if not rows:
        print("Queue is empty.")
        return
    print(f"{'id':>4}  {'ticker':<7} {'period':<7} {'status':<15} {'drafter':<22} created")
    for r in rows:
        print(
            f"{r['id']:>4}  {r['ticker']:<7} {r['period']:<7} {r['status']:<15} "
            f"{r['drafter'][:22]:<22} {r['created_at']}"
        )


def cmd_show(args) -> None:
    item = ReviewQueue(args.db).get(args.id)
    v = item.verification
    print(f"Draft #{item.id} — {item.fact_sheet.company_name} ({item.ticker}) {item.period} — {item.status}")
    print(
        f"Drafter: {item.drafter}. Verification: {'PASSED' if v['passed'] else 'FAILED'} "
        f"({v['numbers_checked']} numbers, {v['facts_source_checked']} facts re-derived from filings)\n"
    )
    by_sentence: dict[int, list[dict]] = {}
    for issue in v["issues"]:
        if issue["sentence_index"] is not None:
            by_sentence.setdefault(issue["sentence_index"], []).append(issue)
    for i, s in enumerate(item.draft.sentences):
        print(f"[{i:>2}] ({s.section}) {s.text}")
        if s.fact_ids:
            print(f"      cites: {', '.join(s.fact_ids)}")
        for issue in by_sentence.get(i, []):
            print(f"      {issue['level'].upper()}: {issue['code']} — {issue['message']}")
    other = [i for i in v["issues"] if i["sentence_index"] is None and i["level"] != "info"]
    if other:
        print("\nOther findings:")
        for issue in other:
            print(f"  {issue['level'].upper()}: {issue['code']} — {issue['message']}")
    if item.draft.open_questions:
        print("\nOpen questions:\n" + "\n".join(f"  - {q}" for q in item.draft.open_questions))
    if item.draft.declined:
        print("\nDeclined:\n" + "\n".join(f"  - {d}" for d in item.draft.declined))
    if item.reviewer:
        print(f"\nReviewed by {item.reviewer} at {item.reviewed_at}: {item.review_note or ''}")


def cmd_approve(args) -> None:
    ReviewQueue(args.db).approve(args.id, args.reviewer, args.note, override=args.override)
    print(f"Draft #{args.id} approved.")


def cmd_reject(args) -> None:
    ReviewQueue(args.db).reject(args.id, args.reviewer, args.note)
    print(f"Draft #{args.id} rejected.")


def cmd_export(args) -> None:
    markdown = ReviewQueue(args.db).export_markdown(args.id)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        sys.stdout.write(markdown)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="Filing variance commentary agent")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="review queue SQLite file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="run everything end to end on synthetic filings")
    p.add_argument("--quarter", default="2024Q4")
    p.add_argument("--online", action="store_true", help="draft with Claude instead of the offline template")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("draft", help="draft commentary for one company-quarter and queue it for review")
    p.add_argument("--ticker", required=True)
    p.add_argument("--quarter", required=True, help="calendar quarter, e.g. 2024Q2")
    p.add_argument("--offline", action="store_true", help="use the deterministic template drafter")
    p.add_argument("--request", help="analyst instructions, e.g. 'focus on margin compression'")
    p.add_argument("--model", help="Claude model ID (default: claude-opus-5-5)")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("eval", help="measure the verifier's recall, or a drafter's pass rate")
    p.add_argument("mode", choices=["verifier", "drafter"])
    p.add_argument("--ticker", default="INTC", help="verifier mode: the company-quarter to mutate")
    p.add_argument("--quarter", default="2024Q2", help="verifier mode")
    p.add_argument("--tickers", default="INTC,AMD,NVDA,MU,TXN", help="drafter mode: comma-separated")
    p.add_argument("--quarters", default="2024Q2,2024Q3,2024Q4,2025Q1", help="drafter mode: comma-separated")
    p.add_argument("--offline", action="store_true", help="drafter mode: template drafter, no API spend")
    p.add_argument("--model", help="drafter mode: Claude model ID (default: claude-opus-5-5)")
    p.add_argument("--out", type=Path, help="also write the full report as JSON")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--driver-memory", help="e.g. 5g")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("queue", help="list drafts")
    p.add_argument("--status", choices=["pending_review", "blocked", "approved", "rejected"])
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("show", help="show a draft with its citations and verifier findings")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve")
    p.add_argument("id", type=int)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note")
    p.add_argument("--override", action="store_true", help="approve a draft that failed verification (needs --note)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject")
    p.add_argument("id", type=int)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("export", help="export approved commentary as markdown with filing footnotes")
    p.add_argument("id", type=int)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ReviewError as e:
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    main()
