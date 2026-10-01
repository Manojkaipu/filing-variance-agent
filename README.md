# filing-variance-agent

Drafts quarterly FP&A variance commentary from SEC filings, and checks
every number in the draft twice before a person reviews it.

Not affiliated with, endorsed by, or built for Intel Corporation. This is an
independent project that uses public SEC filings from Intel and its peers as
a realistic dataset.

## The problem

Every quarter, a financial planning and analysis (FP&A) team writes "variance
commentary": why revenue moved, why margins moved, and how the quarter
compares with last year and with peers. The numbers already exist in public
filings, and the writing follows a structured pattern each quarter. An LLM
can produce a good first draft. The risk is a draft that reads well but gets
a number wrong.

This project produces the first draft with an LLM, but the LLM is never
trusted:

- **Every number comes from a fact sheet.** The fact sheet is built from SEC
  XBRL data, and each fact has an ID that traces back to an accession number,
  XBRL tag and period.
- **Every number is checked twice by deterministic code.** Check 1 compares
  the prose to the cited facts. Check 2 compares each cited fact to the raw
  filing rows.
- **Nothing is usable until a person approves it.** A draft that fails
  verification is blocked. Approving it anyway needs a written override,
  which goes into the audit trail.

## Architecture

```
SEC EDGAR Financial Statement Data Sets (every XBRL fact, every filer)
      │
      ▼
 [bronze]   raw sub/num/tag files → Parquet, read by column name, one partition per quarter
      │
      ▼
 [silver]   facts_history: every filing's version of every fact (restatement history)
      │     facts_panel:   one value per company × metric × calendar quarter
      │                    · tag priority within a filing (ASC 606 tags, fallbacks)
      │                    · Q4 = annual − nine-month YTD, matched on period dates
      │                    · fiscal → calendar quarter by period midpoint
      │                    · lineage on every row: accession number(s), XBRL tag(s), derivation
      ▼
 [gold]     company_quarter: margins, gap-safe QoQ/YoY · sector_benchmark: SIC 3674 quartiles
      │     anomaly_log: identities, YTD consistency, scale errors, sign anomalies,
      │                  restatements, concept changes
      │     tableau_export/*.csv
      ▼
 [agent]    fact sheet (≈70 cited facts) → drafter (Claude, or offline template) → Draft
      │                                     every sentence lists the fact IDs it uses
      ▼
 [verifier] check 1: each $ / % / bps in the prose = correct rounding of a cited fact
      │               (or a change between cited facts), with the right direction
      │     check 2: each cited fact re-derived from raw bronze rows by accession number
      │     policy:  non-GAAP and forward-looking language fail; causal claims are warnings
      ▼
 [review]   SQLite queue: pending_review / blocked → approve · reject → markdown with
      │     footnotes linking each figure to its EDGAR filing
      ▼
 [eval]     inject one defect at a time → recall · meaning-preserving rewrites → specificity
```

## Measured results

Run on 14 quarters of real SEC filings (2022q1–2025q2, 8 GB of
Financial Statement Data Sets):

| | |
|---|---|
| XBRL facts ingested | **48,328,852** |
| Facts mapped to tracked metrics | 789,816 |
| Company-quarters produced | 127,050 across **7,727 filers** |
| Anomalies flagged | 88,429 |
| Pipeline wall-clock | ~10 min, 8-core laptop, 5 GB Spark driver |

**Does the verifier actually catch wrong numbers?** The eval injects one
specific defect at a time into a draft that already passes, and requires the
error code that defect should trigger — then applies meaning-preserving
rewrites and requires silence. On real Intel 2024Q4 data
([full report](docs/eval_verifier.json)):

```
recall:      15/15 injected defects caught
specificity:   5/5 valid rewrites passed untouched
```

The defect classes are a figure off by 2%, by 1000×, and in its last written
digit; a percentage and a basis-point move overstated; a direction word
flipped; citations dropped, invented, and repointed at a real-but-different
fact; a tampered filing row; a ratio that no longer equals its own inputs;
non-GAAP and forward-looking language; a bare numeral; and a number smuggled
into an open question. `python -m src.cli eval verifier` reproduces it.

**What real data changed about the code.** Two rules were wrong in ways only
real filings exposed:

- The restatement rule over-fired on **28% of its flags** (12,586 of 44,404).
  They were companies switching XBRL concept — `StockholdersEquity` →
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
  (8,972 cases), `NetIncomeLoss` → `ProfitLoss` (2,036) — not restating a
  value. Those are now a separate `concept_change` finding, and the surviving
  restatements are real: an operating income that flipped sign between
  filings, and one restated from `983` to `983,000`, a filer fixing its own
  thousands-vs-dollars error.
- The sector median was computed across every SIC 3674 filer, so Intel's
  2.9% operating margin came out *above* the "sector median" of 0.1% — true
  only because the tail includes micro-caps like Wolfspeed at −179%. With a
  configurable revenue floor (default $50M/quarter, ~50 filers), the median
  is 6.0% and Intel sits below it. The floor and filer count are written onto
  every benchmark row so a median always carries its population.

[docs/sample_commentary_intc.md](docs/sample_commentary_intc.md) is generated
end to end from Intel's real filings, every figure footnoted to its EDGAR
filing index.

## Quickstart

```bash
pip install -r requirements.txt      # Python 3.10+, Java 17+ (Spark)
python -m src.cli demo               # synthetic filings → pipeline → draft → verify → queue
python -m src.cli show 1             # the draft, its citations, and verifier findings
python -m src.cli approve 1 --reviewer "Your Name"
python -m src.cli export 1 --out commentary.md
```

`demo` needs no network or API key. It writes SEC-format files for five
**fictional** companies, runs the full Spark pipeline, and drafts with the
deterministic template drafter. The output is in
[docs/sample_commentary.md](docs/sample_commentary.md). The fictional
companies are built to hit the hard cases:

- a January fiscal year-end (like NVIDIA)
- a 53-week year ending January 4
- a restatement
- a revenue figure reported in thousands instead of dollars
- a non-semiconductor filer that must stay out of the sector benchmark
- an older `num.txt` column layout

### On real SEC data

```bash
export EDGAR_CONTACT_EMAIL="you@example.com"   # the SEC requires a contact email in the User-Agent
python -m src.download.edgar_downloader --start-quarter 2022q1 --end-quarter 2025q2
python -m src.pipeline.run --driver-memory 5g --shuffle-partitions 32

export ANTHROPIC_API_KEY=...                   # or an `ant auth login` profile
python -m src.cli draft --ticker INTC --quarter 2024Q4 --driver-memory 4g
python -m src.cli draft --ticker INTC --quarter 2024Q4 --request "Focus on gross margin"
python -m src.cli draft --ticker INTC --quarter 2024Q4 --offline   # no API key needed
```

`./scripts/run_pipeline.sh` does the download and pipeline in one step using
the full range in `config/companies.yaml`. Each quarter is roughly 80–130 MB
zipped; 14 quarters extract to about 8 GB. Spark's default 1 GB driver heap
is not enough for a full-filer run — pass `--driver-memory`.

### Measuring it yourself

```bash
python -m src.cli eval verifier --ticker INTC --quarter 2024Q4   # free, no API calls
python -m src.cli eval drafter --tickers INTC,AMD,NVDA --quarters 2024Q3,2024Q4
python -m src.cli eval drafter --offline --quarters 2024Q4       # template baseline, free
```

The company universe (Intel plus nine peers) and the sector SIC code are set
in [config/companies.yaml](config/companies.yaml). Tickers resolve to CIKs
through the SEC's own ticker file, so no CIK is typed by hand.

## How the verifier works

| Check | What it catches | Example |
|---|---|---|
| Number ↔ cited fact | invented or misread figures, wrong rounding | "$11.4 billion" when the cited fact is $11.000B → `number_mismatch` |
| Recomputed changes | arithmetic errors in % changes and margin moves | "up 310 bps" when the cited margins differ by 300 bps |
| Direction | "rose" for a decline | "revenue fell 10.0%" citing a +10.0% change → `direction_mismatch` |
| Citations | numbers with no source, made-up fact IDs | `uncited_number`, `unknown_fact_id` |
| Stray numerals | counts and figures in a format the parser can't verify | "3 straight quarters" → spell out the count |
| Fact ↔ filing | pipeline bugs between raw data and the fact sheet | the filing says $10.40B but the fact sheet says $10.50B → `source_mismatch` |
| Derived values | broken Q4, gross-profit or ratio derivations | Q4 is recomputed from the annual 10-K and Q3 10-Q rows |
| Policy | non-GAAP, guidance, forward-looking claims | "Management expects…" → `forward_looking` |
| Causal language (warning) | drivers the filings can't support | "driven by AI demand" → the reviewer must confirm |

The Claude drafter (`claude-opus-5-5`, structured JSON output) gets the fact
sheet and the anomaly log. Its prompt tells it to explain moves only through
arithmetic the facts support, and to send questions about business drivers to
an `open_questions` list rather than invent them. Server-side refusal
fallbacks are on (`fallbacks: "default"`); a refusal or truncated output
raises an error and is never parsed as a draft.

## Key design decisions

- **Verification is deterministic, not a second LLM.** A grader model can be
  persuaded by fluent prose. A parser that converts "$12.83 billion" to
  12.83e9 ± 0.005e9 and compares it with a sourced value cannot.
- **Check 2 reads bronze, not gold.** The fact sheet is built through silver
  and gold. The verifier looks each value up again in the raw SEC rows by
  accession number + tag + period. That path doesn't share code with the
  pipeline, so it catches pipeline bugs as well as LLM mistakes.
- **Calendar quarters by period midpoint, without a per-company lookup table.**
  Each fact is bucketed by its period-end date minus 45 days. NVIDIA's quarter
  ending 2024-01-28 lands in calendar 2023Q4, and a 53-week year ending
  2025-01-04 stays in 2024Q4. A month-of-period-end rule gets both wrong
  ([details](docs/metric_dictionary.md#fiscal-calendar-alignment)).
- **Q4 is derived from period dates, never from the filing's `fy`.** A 10-K
  carries the prior year's annual figures with the same `fy`, so joining on
  `fy` subtracts the wrong year's nine-month figure. If either side is
  missing, no Q4 row is produced rather than a guess.
- **Restatements are kept, not overwritten.** `facts_history` keeps every
  filing's version. The panel uses the latest-filed value, and the anomaly log
  records the as-first-reported value next to it.
- **Anomalies are flagged, not silently fixed.** A value that looks 1000× too
  small is logged, not rescaled. The agent shows the flag to the reviewer as a
  data caveat.
- **Derived values are labeled.** A derived Q4 or gross profit carries
  `is_derived=True` and both source accession numbers. The commentary says
  when a headline figure was derived.

## Testing

```bash
pytest            # 114 tests: unit, Spark, and an end-to-end run on synthetic filings
ruff check src tests
```

Tests use synthetic data with answers worked out by hand. For example,
"annual $54.228B minus nine-month $38.812B must derive a Q4 of $15.416B", and
"citing a +10.0% change in a sentence that says 'fell' must fail". There are
also regression tests for each correctness bug fixed in the pipeline:
quarters overwriting each other in bronze, Q4 matched across the wrong year,
restatement history lost to early de-duplication, QoQ computed across a
missing quarter, a concept change misread as a restatement, a revenue floor
that changes which median you get, and the YAML coercion that turned ON
Semiconductor's ticker into the boolean `true`. The Claude drafter is tested
with a stubbed client: request shape, refusal handling, and a hallucinated
number caught downstream. The eval harness is tested too — a mutation that
silently stopped applying would push recall toward 100% for the wrong
reason, so "every mutation still applies" is its own assertion. CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, tests and
the demo on Ubuntu.

> **Windows:** Spark needs Hadoop's `winutils.exe` and `hadoop.dll` in
> `%HADOOP_HOME%\bin` to write files. Without them, the 6 tests that write
> Parquet are skipped with a message and the other 70 still run. WSL or
> Linux is the easiest setup.

## Project layout

```
src/download/edgar_downloader.py   SEC ticker→CIK map, quarterly dataset download (rate-limited, User-Agent)
src/pipeline/{bronze,silver,gold}.py   medallion layers; run.py orchestrates them
src/anomaly/rules.py               anomaly rules, each a pure DataFrame → log-rows function
src/agent/facts.py                 FactSheet: cited facts with lineage and formulas
src/agent/drafter.py               ClaudeDrafter (structured output) and TemplateDrafter
src/agent/verifier.py              number extraction, both checks, policy rules
src/review/queue.py                SQLite review queue, approval rules, markdown export
src/eval/mutations.py              injected defects + meaning-preserving rewrites
src/eval/harness.py                recall/specificity and drafter pass-rate measurement
src/demo/synthetic_filings.py      fictional SEC-format filings for the demo and e2e tests
src/cli.py                         demo / draft / eval / queue / show / approve / reject / export
```

## Known limitations

- **Consolidated GAAP facts only.** Segment breakdowns (the `segments` field)
  and non-GAAP measures are out of scope, so the commentary can explain
  dollar moves but not segment mix or price/volume effects.
- **Tag coverage is good but not complete.** Across SIC 3674 filer-quarters
  that reported revenue, coverage is 100% revenue, 97% gross profit, 99%
  operating income, 98% net income, 88% R&D — but only 73% total liabilities,
  because many filers tag only "liabilities and equity". Every number the
  agent cites is traced to a filing, so a gap shows up as a missing fact
  rather than a wrong one.
- **Peer and sector values aren't traced to individual filings.** They are
  aggregates from the gold layer; the verifier marks them
  `aggregate_not_traced` rather than claiming filing-level verification.
- **The sector median is a judgment call, not a fact.** It depends on the
  revenue floor, as the numbers above show. The floor is explicit and
  recorded, but "the sector" is still a choice the reader should see.
- **Early quarters are thin.** The quarterly datasets carry comparative
  columns, so periods before the downloaded range exist but with few filers
  (3 filers in 2020Q3 vs ~50 from 2021Q1). `filer_count` makes this visible;
  don't read a median built from three companies.
- **Verifier recall is measured against the defect classes I thought of.**
  15/15 is a statement about those 15 classes on this data, not a guarantee.
  A defect nobody modelled — a plausible-but-wrong causal claim that reads as
  hedged, say — would pass check 1 and check 2 and land on the reviewer.
- **The Claude drafter's pass rate on real filings is not yet measured.** The
  deterministic drafter passes by construction, and the LLM path is tested
  against a stubbed client, but `eval drafter` has not been run against the
  API at scale. That is the one number still missing.

## Disclaimer

Built as an independent learning project. All real data comes from the SEC's
public EDGAR system. The demo companies and their figures are fictional.
Nothing here represents any company's internal data, forecasts, or non-public
information.
