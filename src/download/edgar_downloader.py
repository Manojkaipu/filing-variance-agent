"""
Downloads SEC data for filing-variance-agent.

Two things get pulled:
  1. The SEC's quarterly "Financial Statement Data Sets" — zip files of every
     XBRL fact filed by every public company that quarter. This is the bulk
     numeric data the Spark pipeline processes.
  2. company_tickers.json — the SEC's own ticker -> CIK mapping, used so we
     never hand-type a CIK number.

SEC rules this script follows (see https://www.sec.gov/os/webmaster-faq#developers):
  - Every request must send a descriptive User-Agent with a real contact
    email. Set EDGAR_CONTACT_EMAIL before running.
  - No more than ~10 requests/second. We stay well under that with
    REQUEST_DELAY_SECONDS between calls, since we're downloading a handful
    of large files, not making many small ones.

Usage:
    export EDGAR_CONTACT_EMAIL="you@example.com"
    python -m src.download.edgar_downloader
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from pathlib import Path

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
REQUEST_DELAY_SECONDS = 0.5  # well under the SEC's ~10 req/sec limit

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_DATASET_URL_TEMPLATE = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"


def _require_contact_email() -> str:
    email = os.environ.get("EDGAR_CONTACT_EMAIL")
    if not email or "@" not in email:
        raise RuntimeError(
            "Set EDGAR_CONTACT_EMAIL to a real contact email before downloading. "
            "The SEC requires a descriptive User-Agent on every request and will "
            "block generic or missing ones."
        )
    return email


def _headers() -> dict:
    email = _require_contact_email()
    return {"User-Agent": f"filing-variance-agent research project ({email})"}


def load_companies_config() -> dict:
    with open(PROJECT_ROOT / "config" / "companies.yaml") as f:
        cfg = yaml.safe_load(f)
    _validate_tickers(cfg)
    return cfg


def _validate_tickers(cfg: dict) -> None:
    """Fails loudly on a ticker YAML didn't give us as a string. In YAML 1.1
    the bare words ON, OFF, YES, NO, Y and N are booleans, so an unquoted
    `ticker: ON` (ON Semiconductor) arrives as True and would otherwise
    surface much later as a baffling "could not resolve CIK for [True]"."""
    for entry in [cfg["focal_company"], *cfg["peers"]]:
        ticker = entry.get("ticker")
        if not isinstance(ticker, str):
            raise RuntimeError(
                f"Ticker for {entry.get('name', entry)!r} parsed as {ticker!r} ({type(ticker).__name__}), "
                "not a string. Quote it in config/companies.yaml — YAML reads bare ON/OFF/YES/NO as booleans."
            )


def fetch_ticker_to_cik_map(force_refresh: bool = False) -> dict:
    """Returns {ticker: zero-padded 10-digit CIK string}. Cached locally."""
    cache_path = RAW_DIR / "company_tickers.json"
    if cache_path.exists() and not force_refresh:
        raw = json.loads(cache_path.read_text())
    else:
        resp = requests.get(SEC_TICKERS_URL, headers=_headers(), timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw))
        time.sleep(REQUEST_DELAY_SECONDS)

    # The SEC file is keyed by arbitrary integer, each value like:
    # {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
    return {entry["ticker"].upper(): str(entry["cik_str"]).zfill(10) for entry in raw.values()}


def resolve_company_ciks() -> dict:
    """Returns {ticker: cik} for the focal company + peers in companies.yaml."""
    cfg = load_companies_config()
    tickers = [cfg["focal_company"]["ticker"]] + [p["ticker"] for p in cfg["peers"]]
    ticker_to_cik = fetch_ticker_to_cik_map()

    resolved, missing = {}, []
    for t in tickers:
        if t in ticker_to_cik:
            resolved[t] = ticker_to_cik[t]
        else:
            missing.append(t)
    if missing:
        raise RuntimeError(
            f"Could not resolve CIK for tickers: {missing}. "
            "Check spelling in config/companies.yaml, or the SEC's ticker "
            "file may use a different symbol for a recent listing change."
        )
    return resolved


def quarter_range(start_quarter: str, end_quarter: str) -> list[str]:
    """e.g. quarter_range('2018q1', '2019q2') -> ['2018q1', ..., '2019q2']"""

    def parse(q: str) -> tuple[int, int]:
        year, qtr = q.split("q")
        return int(year), int(qtr)

    start_y, start_q = parse(start_quarter)
    end_y, end_q = parse(end_quarter)
    quarters = []
    y, q = start_y, start_q
    while (y, q) <= (end_y, end_q):
        quarters.append(f"{y}q{q}")
        q += 1
        if q > 4:
            q = 1
            y += 1
    return quarters


def download_quarter(quarter: str, dest_dir: Path = RAW_DIR) -> Path:
    """Downloads one quarterly Financial Statement Data Set zip if not already present."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{quarter}.zip"
    if zip_path.exists():
        return zip_path

    url = SEC_DATASET_URL_TEMPLATE.format(quarter=quarter)
    resp = requests.get(url, headers=_headers(), stream=True, timeout=120)
    if resp.status_code == 404:
        raise FileNotFoundError(
            f"No dataset published for {quarter} at {url}. "
            "The SEC publishes these roughly six weeks after quarter-end, so "
            "the most recent one or two quarters may not exist yet."
        )
    resp.raise_for_status()

    # Write to a temp name and rename once complete, so an interrupted
    # download never leaves a truncated zip that later runs treat as done.
    partial = zip_path.with_suffix(".zip.part")
    with open(partial, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    partial.replace(zip_path)
    time.sleep(REQUEST_DELAY_SECONDS)
    return zip_path


def extract_quarter(zip_path: Path) -> Path:
    """Extracts sub.txt, num.txt, tag.txt into data/raw/<quarter>/"""
    quarter = zip_path.stem
    out_dir = zip_path.parent / quarter
    out_dir.mkdir(exist_ok=True)
    wanted = {"sub.txt", "num.txt", "tag.txt"}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name in wanted:
                z.extract(name, out_dir)
    return out_dir


def download_all(
    start_quarter: str | None = None, end_quarter: str | None = None, dest_dir: Path = RAW_DIR
) -> list[Path]:
    cfg = load_companies_config()
    start_quarter = start_quarter or cfg["start_quarter"]
    end_quarter = end_quarter or cfg["end_quarter"]

    extracted_dirs = []
    for quarter in quarter_range(start_quarter, end_quarter):
        try:
            zip_path = download_quarter(quarter, dest_dir)
        except FileNotFoundError as e:
            print(f"Skipping {quarter}: {e}", flush=True)
            continue
        extracted_dirs.append(extract_quarter(zip_path))
        size_mb = zip_path.stat().st_size / 1e6
        print(f"Downloaded and extracted {quarter} ({size_mb:,.0f} MB)", flush=True)
    return extracted_dirs


def main(argv: list[str] | None = None) -> None:
    cfg = load_companies_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-quarter", default=cfg["start_quarter"], help="e.g. 2022q1")
    parser.add_argument("--end-quarter", default=cfg["end_quarter"], help="e.g. 2025q2")
    parser.add_argument("--dest-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--skip-ciks", action="store_true", help="don't resolve tickers to CIKs first")
    args = parser.parse_args(argv)

    if not args.skip_ciks:
        print("Resolved CIKs:", resolve_company_ciks(), flush=True)
    quarters = quarter_range(args.start_quarter, args.end_quarter)
    print(f"Downloading {len(quarters)} quarters: {quarters[0]}..{quarters[-1]}", flush=True)
    download_all(args.start_quarter, args.end_quarter, args.dest_dir)


if __name__ == "__main__":
    main()
