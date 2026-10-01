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
      │     anomaly_log: identities, YTD consistency, scale errors, sign anomalies, restatements
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
            footnotes linking each figure to its EDGAR filing
```

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
./scripts/run_pipeline.sh                      # download 2018q1–2025q4, then run the pipeline
export ANTHROPIC_API_KEY=...                   # or an `ant auth login` profile
python -m src.cli draft --ticker INTC --quarter 2024Q2
python -m src.cli draft --ticker INTC --quarter 2024Q2 --request "Focus on gross margin"
python -m src.cli draft --ticker INTC --quarter 2024Q2 --offline   # template drafter
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
pytest            # 76 tests: unit, Spark, and an end-to-end run on synthetic filings
ruff check src tests
```

Tests use synthetic data with answers worked out by hand. For example,
"annual $54.228B minus nine-month $38.812B must derive a Q4 of $15.416B", and
"citing a +10.0% change in a sentence that says 'fell' must fail". There are
also regression tests for each correctness bug fixed in the pipeline:
quarters overwriting each other in bronze, Q4 matched across the wrong year,
restatement history lost to early de-duplication, and QoQ computed across a
missing quarter. The Claude drafter is tested with a stubbed client: request
shape, refusal handling, and a hallucinated number caught downstream. CI
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
src/demo/synthetic_filings.py      fictional SEC-format filings for the demo and e2e tests
src/cli.py                         demo / draft / queue / show / approve / reject / export
```

## Known limitations

- **Consolidated GAAP facts only.** Segment breakdowns (the `segments` field)
  and non-GAAP measures are out of scope, so the commentary can explain
  dollar moves but not segment mix or price/volume effects.
- **The metric dictionary's tags still need checking against real filings.**
  See [config/metric_dictionary.yaml](config/metric_dictionary.yaml). The
  sector benchmark is only as complete as that tag coverage.
- **Peer and sector values aren't traced to individual filings.** They are
  aggregates from the gold layer; the verifier marks them as such rather than
  claiming filing-level verification.
- **The LLM path hasn't been evaluated at scale.** The verifier is the safety
  net, but how often Claude's drafts pass verification without edits on real
  filings hasn't been measured yet. That's the natural next step: an eval set
  of company-quarters, scored by pass rate and reviewer edits.

## Disclaimer

Built as an independent learning project. All real data comes from the SEC's
public EDGAR system. The demo companies and their figures are fictional.
Nothing here represents any company's internal data, forecasts, or non-public
information.
