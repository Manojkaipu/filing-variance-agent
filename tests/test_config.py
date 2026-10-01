"""The configured universe has to survive YAML's type coercion and the
quarter-range arithmetic the downloader builds its URLs from."""

import pytest
import yaml

from src.download.edgar_downloader import _validate_tickers, load_companies_config, quarter_range


def test_every_configured_ticker_is_a_string():
    # ON Semiconductor's ticker is the YAML 1.1 boolean `on` unless quoted.
    cfg = load_companies_config()
    tickers = [cfg["focal_company"]["ticker"]] + [p["ticker"] for p in cfg["peers"]]
    assert all(isinstance(t, str) for t in tickers), tickers
    assert "ON" in tickers


def test_unquoted_boolean_ticker_fails_with_a_clear_message():
    cfg = yaml.safe_load("focal_company:\n  ticker: ON\n  name: ON Semiconductor\npeers: []\n")
    with pytest.raises(RuntimeError, match="Quote it in config/companies.yaml"):
        _validate_tickers(cfg)


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2024q1", "2024q1", ["2024q1"]),
        ("2023q3", "2024q2", ["2023q3", "2023q4", "2024q1", "2024q2"]),
        ("2024q4", "2025q1", ["2024q4", "2025q1"]),
    ],
)
def test_quarter_range_crosses_year_boundaries(start, end, expected):
    assert quarter_range(start, end) == expected


def test_quarter_range_is_empty_when_end_precedes_start():
    assert quarter_range("2024q3", "2024q1") == []
