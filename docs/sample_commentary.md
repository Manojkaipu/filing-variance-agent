> **Sample output from `python -m src.cli demo`.** Demo Silicon Corp, its peers, and every figure
> below are fictional (see `src/demo/synthetic_filings.py`). The EDGAR links show the citation
> format; they use made-up accession numbers and do not resolve.

# Demo Silicon Corp (DEMO) — 2024Q4 variance commentary

*Approved by Demo Reviewer on 2026-10-01T01:40:06+00:00. Drafted by `template-v1`; GAAP figures from SEC XBRL filings.*

## Headline

Demo Silicon Corp reported 2024Q4 revenue of $14.37 billion, up 0.9% year over year.[^1][^2]

## Revenue

Sequentially, revenue was up 4.2% from 2024Q3.[^3]

## Margins

Gross margin was 39.5%, which contracted by 249 basis points from 2023Q4.[^4][^5] Gross profit was down 5.1% year over year against a 0.9% increase in revenue, compressing gross margin.[^6][^2]

## Operating expenses

R&D expense was $3.45 billion, or 24.0% of revenue.[^7][^8]

## Profitability

Operating income was $790 million, an operating margin of 5.5%.[^9][^10] Operating margin moved down 219 basis points year over year.[^11] Net income was $648 million.[^12]

## Peer and sector context

Gross margin sat below the sector median of 48.5%.[^4][^13] Operating margin was below the sector median of 18.5%.[^10][^14] All three named peers with data this quarter reported a higher gross margin.

## Data caveats

Fourth-quarter revenue is not disclosed on its own in XBRL; the pipeline derived it as full-year revenue minus nine-month year-to-date revenue. The anomaly log flags a restatement or recast (the panel uses the latest-filed values) for 2023Q4 (revenue, cost of revenue, R&D, SG&A, operating income, net income); comparisons against that quarter should be confirmed by the reviewer.

## Open questions for the business

- What business drivers explain the year-over-year revenue change?
- Were there one-time items in cost of revenue or operating expenses this quarter?

---

[^1]: `DEMO.revenue.2024Q4` = $14.367B; XBRL `RevenueFromContractWithCustomerExcludingAssessedTax`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000900001-25-000024](https://www.sec.gov/Archives/edgar/data/900001/000090000125000024/0000900001-25-000024-index.htm), [0000900001-24-000023](https://www.sec.gov/Archives/edgar/data/900001/000090000124000023/0000900001-24-000023-index.htm).
[^2]: `DEMO.revenue_yoy_pct.2024Q4` = 0.86%, computed as pct_change(DEMO.revenue.2024Q4, DEMO.revenue.2023Q4).
[^3]: `DEMO.revenue_qoq_pct.2024Q4` = 4.18%, computed as pct_change(DEMO.revenue.2024Q4, DEMO.revenue.2024Q3).
[^4]: `DEMO.gross_margin_pct.2024Q4` = 39.50%, computed as ratio(DEMO.gross_profit.2024Q4, DEMO.revenue.2024Q4).
[^5]: `DEMO.gross_margin_pct_yoy_change.2024Q4` = -2.49 pp, computed as difference(DEMO.gross_margin_pct.2024Q4, DEMO.gross_margin_pct.2023Q4).
[^6]: `DEMO.gross_profit_yoy_pct.2024Q4` = -5.12%, computed as pct_change(DEMO.gross_profit.2024Q4, DEMO.gross_profit.2023Q4).
[^7]: `DEMO.research_and_development_expense.2024Q4` = $3.448B; XBRL `ResearchAndDevelopmentExpense`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000900001-25-000024](https://www.sec.gov/Archives/edgar/data/900001/000090000125000024/0000900001-25-000024-index.htm), [0000900001-24-000023](https://www.sec.gov/Archives/edgar/data/900001/000090000124000023/0000900001-24-000023-index.htm).
[^8]: `DEMO.rd_intensity_pct.2024Q4` = 24.00%, computed as ratio(DEMO.research_and_development_expense.2024Q4, DEMO.revenue.2024Q4).
[^9]: `DEMO.operating_income.2024Q4` = $790.00M; XBRL `OperatingIncomeLoss`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000900001-25-000024](https://www.sec.gov/Archives/edgar/data/900001/000090000125000024/0000900001-25-000024-index.htm), [0000900001-24-000023](https://www.sec.gov/Archives/edgar/data/900001/000090000124000023/0000900001-24-000023-index.htm).
[^10]: `DEMO.operating_margin_pct.2024Q4` = 5.50%, computed as ratio(DEMO.operating_income.2024Q4, DEMO.revenue.2024Q4).
[^11]: `DEMO.operating_margin_pct_yoy_change.2024Q4` = -2.19 pp, computed as difference(DEMO.operating_margin_pct.2024Q4, DEMO.operating_margin_pct.2023Q4).
[^12]: `DEMO.net_income.2024Q4` = $648.00M; XBRL `NetIncomeLoss`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000900001-25-000024](https://www.sec.gov/Archives/edgar/data/900001/000090000125000024/0000900001-25-000024-index.htm), [0000900001-24-000023](https://www.sec.gov/Archives/edgar/data/900001/000090000124000023/0000900001-24-000023-index.htm).
[^13]: `SECTOR.gross_margin_pct_median.2024Q4` = 48.50%; SIC 3674, 4 filers.
[^14]: `SECTOR.operating_margin_pct_median.2024Q4` = 18.49%; SIC 3674, 4 filers.
