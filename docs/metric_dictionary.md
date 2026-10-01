# Metric Dictionary — filing-variance-agent

This is the canonical definition of every metric the pipeline computes, and
which XBRL tag(s) on a real filing can carry it. It exists because different
companies — and the same company in different years — use different XBRL
tags for the same economic concept. Without this dictionary, a naive `SUM`
over the raw `num` table silently drops or double-counts facts.

Status: **draft, not yet verified against real filings.** Verify every tag
here against actual `tag.txt` / `num.txt` files once Phase 1 downloads real
data, and update `config/metric_dictionary.yaml` (the machine-readable
version this file mirrors) as you find exceptions. Log every correction in
the "Revision log" section at the bottom — that log is itself evidence of
the "detect and correct data anomalies" line in the JD.

## Filing types

| Form | Meaning | Frequency |
|---|---|---|
| 10-K | Annual report | Once a year |
| 10-Q | Quarterly report | Three times a year (Q4 is implied, not filed standalone) |
| 10-K/A, 10-Q/A | Amendment | As needed — signals a restatement or recast |

## Duration codes (`qtrs` field in `num.txt`)

| `qtrs` | Meaning |
|---|---|
| 0 | Point in time (balance sheet items: assets, liabilities, equity) |
| 1 | One quarter (income statement items in a 10-Q) |
| 3 | Nine months, year-to-date (common in Q3 10-Qs for YTD columns) |
| 4 | Full fiscal year (income statement items in a 10-K) |

**Q4 is almost never reported as a standalone quarter.** It is derived as:
`Q4 = Annual (qtrs=4) − nine-month YTD (qtrs=3)`,
matched on company, metric, and period end dates (the YTD period must end
80–100 days before the annual one). Matching on the filing's `fy` field is
wrong: a 10-K carries two or three years of comparative annual figures that
all share one `fy`. See `src/pipeline/silver.py::derive_q4_facts`. The
`ytd_consistency` anomaly rule checks that Q1 + Q2 + Q3 equals the nine-month
figure, which is what makes the derived Q4 trustworthy.

## Canonical metrics

| Metric | Primary tag | Fallback tag(s) | Notes |
|---|---|---|---|
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` | `Revenues`, `SalesRevenueNet` | The fallback `SalesRevenueNet` is the pre-ASC-606 tag (before fiscal 2018). Mixing pre- and post-606 revenue in one YoY comparison is a real error — flag any YoY pair that crosses the ASC 606 adoption quarter. |
| Cost of revenue | `CostOfRevenue` | `CostOfGoodsAndServicesSold` | |
| Gross profit | `GrossProfit` | *(derived: revenue − cost of revenue)* | If a filer doesn't report `GrossProfit` directly, mark the row `derived=true` so it's distinguishable from a disclosed value. |
| R&D expense | `ResearchAndDevelopmentExpense` | — | |
| SG&A expense | `SellingGeneralAndAdministrativeExpense` | `GeneralAndAdministrativeExpense` | Some filers split SG&A into separate selling and G&A lines — sum them if both are present and the combined tag is absent. |
| Operating income | `OperatingIncomeLoss` | — | |
| Net income | `NetIncomeLoss` | `ProfitLoss` | |
| Total assets | `Assets` | — | Point in time. |
| Total liabilities | `Liabilities` | — | Point in time. |
| Stockholders' equity | `StockholdersEquity` | `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | Point in time. |

## Derived ratios

- **Gross margin %** = gross profit / revenue
- **Operating margin %** = operating income / revenue
- **R&D intensity %** = R&D expense / revenue
- **QoQ change** = (current quarter − prior quarter) / prior quarter
- **YoY change** = (current quarter − same quarter, prior year) / same quarter, prior year

## GAAP vs. non-GAAP

XBRL facts are **GAAP only**. Company earnings press releases quote
non-GAAP figures (e.g., "non-GAAP gross margin"), but those numbers are not
tagged in XBRL and have no consistent tag name across filers. The drafter
is instructed to decline non-GAAP requests (recorded in the draft's
`declined` list), and the verifier's policy check fails any sentence that
mentions a non-GAAP measure, so a draft that slips one in can't be approved
without an explicit reviewer override.

## Fiscal calendar alignment

Filers' fiscal years don't all end in December. The pipeline does **not**
hardcode each company's fiscal year-end; instead it buckets every fact into
a calendar quarter based on the fact's own period-end date (`ddate`) shifted
back 45 days — roughly the quarter's midpoint. This keeps the mapping
correct even if a company changes its fiscal calendar, and handles two cases
a plain "month of the period end" rule gets wrong:

| Period end | Covers | Month rule | Midpoint rule |
|---|---|---|---|
| 2024-01-28 (NVIDIA fiscal Q4) | Nov–Jan | 2024Q1 | **2023Q4** |
| 2025-01-04 (53-week year-end) | Oct–Dec | 2025Q1 (collides with the real Q1) | **2024Q4** |
| 2023-04-01 (52-week Q1) | Jan–Mar | 2023Q2 | **2023Q1** |

This matches the sell-side "calendarization" convention (NVIDIA FQ4'24 ≈ CQ4'23).

## Restatements and recasts

- **Restatement**: corrects an error in previously reported figures.
- **Recast**: re-presents prior periods after a structural change (e.g., a
  segment reorganization), without implying the original figures were wrong.

Both appear as the same fact (same tag, company, period) reported with a
different value in a later filing. Silver keeps **every** filing's version
in `facts_history`; the `facts_panel` (and therefore gold and the agent) uses
the **latest-filed** value, so year-over-year comparisons are made on a
restated basis. `src/anomaly/rules.py` logs the as-first-reported vs latest
delta, and the agent surfaces it as a data caveat whenever it touches a
quarter it discusses.

## Revision log

*(Fill in as you verify tags against real filings.)*

| Date | Change | Evidence |
|---|---|---|
| — | — | — |
