# Intel Corporation (INTC) — 2024Q4 variance commentary

*Approved by M. Kaipu on 2026-10-01T02:33:23+00:00. Drafted by `template-v1`; GAAP figures from SEC XBRL filings.*

## Headline

Intel Corporation reported 2024Q4 revenue of $14.26 billion, down 7.4% year over year.[^1][^2]

## Revenue

Sequentially, revenue was up 7.3% from 2024Q3.[^3]

## Margins

Gross margin was 39.2%, which contracted by 658 basis points from 2023Q4.[^4][^5] Gross profit was down 20.8% year over year against a 7.4% decrease in revenue, compressing gross margin.[^6][^2]

## Operating expenses

R&D expense was $3.88 billion, or 27.2% of revenue.[^7][^8]

## Profitability

Operating income was $412 million, an operating margin of 2.9%.[^9][^10] Operating margin moved down 13.9 percentage points year over year.[^11] Net loss was $126 million.[^12]

## Peer and sector context

Gross margin sat below the sector median of 46.5%.[^4][^13] Operating margin was below the sector median of 6.0%.[^10][^14] Eight of the nine named peers with data this quarter reported a higher gross margin.

## Data caveats

Fourth-quarter revenue is not disclosed on its own in XBRL; the pipeline derived it as full-year revenue minus nine-month year-to-date revenue. The anomaly log flags a change in which XBRL concept the company reports, so values either side are not comparable for 2023Q4 (equity); comparisons against that quarter should be confirmed by the reviewer.

## Open questions for the business

- What business drivers explain the year-over-year revenue change?
- Were there one-time items in cost of revenue or operating expenses this quarter?

---

[^1]: `INTC.revenue.2024Q4` = $14.260B; XBRL `RevenueFromContractWithCustomerExcludingAssessedTax`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000050863-25-000009](https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/0000050863-25-000009-index.htm), [0000050863-24-000149](https://www.sec.gov/Archives/edgar/data/50863/000005086324000149/0000050863-24-000149-index.htm).
[^2]: `INTC.revenue_yoy_pct.2024Q4` = -7.44%, computed as pct_change(INTC.revenue.2024Q4, INTC.revenue.2023Q4).
[^3]: `INTC.revenue_qoq_pct.2024Q4` = 7.35%, computed as pct_change(INTC.revenue.2024Q4, INTC.revenue.2024Q3).
[^4]: `INTC.gross_margin_pct.2024Q4` = 39.16%, computed as ratio(INTC.gross_profit.2024Q4, INTC.revenue.2024Q4).
[^5]: `INTC.gross_margin_pct_yoy_change.2024Q4` = -6.58 pp, computed as difference(INTC.gross_margin_pct.2024Q4, INTC.gross_margin_pct.2023Q4).
[^6]: `INTC.gross_profit_yoy_pct.2024Q4` = -20.76%, computed as pct_change(INTC.gross_profit.2024Q4, INTC.gross_profit.2023Q4).
[^7]: `INTC.research_and_development_expense.2024Q4` = $3.876B; XBRL `ResearchAndDevelopmentExpense`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000050863-25-000009](https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/0000050863-25-000009-index.htm), [0000050863-24-000149](https://www.sec.gov/Archives/edgar/data/50863/000005086324000149/0000050863-24-000149-index.htm).
[^8]: `INTC.rd_intensity_pct.2024Q4` = 27.18%, computed as ratio(INTC.research_and_development_expense.2024Q4, INTC.revenue.2024Q4).
[^9]: `INTC.operating_income.2024Q4` = $412.00M; XBRL `OperatingIncomeLoss`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000050863-25-000009](https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/0000050863-25-000009-index.htm), [0000050863-24-000149](https://www.sec.gov/Archives/edgar/data/50863/000005086324000149/0000050863-24-000149-index.htm).
[^10]: `INTC.operating_margin_pct.2024Q4` = 2.89%, computed as ratio(INTC.operating_income.2024Q4, INTC.revenue.2024Q4).
[^11]: `INTC.operating_margin_pct_yoy_change.2024Q4` = -13.89 pp, computed as difference(INTC.operating_margin_pct.2024Q4, INTC.operating_margin_pct.2023Q4).
[^12]: `INTC.net_income.2024Q4` = -$126.00M; XBRL `NetIncomeLoss`, period ending 2024-12-31 (derived: annual minus ytd9); filings [0000050863-25-000009](https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/0000050863-25-000009-index.htm), [0000050863-24-000149](https://www.sec.gov/Archives/edgar/data/50863/000005086324000149/0000050863-24-000149-index.htm).
[^13]: `SECTOR.gross_margin_pct_median.2024Q4` = 46.54%; SIC 3674, 51 filers with quarterly revenue above $50.00M.
[^14]: `SECTOR.operating_margin_pct_median.2024Q4` = 5.95%; SIC 3674, 51 filers with quarterly revenue above $50.00M.
