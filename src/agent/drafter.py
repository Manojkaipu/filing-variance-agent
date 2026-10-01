"""
Drafts quarterly variance commentary from a FactSheet.

Two interchangeable drafters share one output shape (`Draft`):

  - ClaudeDrafter: calls Claude with the fact sheet and a JSON schema. Every
    sentence comes back paired with the fact IDs it relies on, so the
    verifier can check each number against its cited source.
  - TemplateDrafter: deterministic, rule-based commentary from the same fact
    sheet. Needs no API key, so the full pipeline (draft -> verify -> review)
    runs offline and in tests; also a useful baseline to compare the LLM to.

Neither drafter is trusted. Their output always goes through
src/agent/verifier.py and then the human review queue.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Protocol

from src.agent.facts import Fact, FactSheet, fact_id

SECTIONS = ["headline", "revenue", "margins", "operating_expenses", "profitability", "peer_context", "data_caveats"]
DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class Sentence:
    section: str
    text: str
    fact_ids: list[str]


@dataclass
class Draft:
    sentences: list[Sentence]
    open_questions: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    drafter: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Draft:
        data = json.loads(raw)
        return cls(**{**data, "sentences": [Sentence(**s) for s in data["sentences"]]})

    @classmethod
    def from_model_output(cls, data: dict, drafter: str) -> Draft:
        return cls(
            sentences=[Sentence(s["section"], s["text"], list(s["fact_ids"])) for s in data["sentences"]],
            open_questions=list(data.get("open_questions", [])),
            declined=list(data.get("declined", [])),
            drafter=drafter,
        )


class Drafter(Protocol):
    def draft(self, sheet: FactSheet, analyst_request: str | None = None) -> Draft: ...


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are drafting the first version of quarterly variance commentary for a \
financial planning and analysis (FP&A) team. A human analyst reviews every \
draft before anyone uses it, and an automated verifier checks every number \
you write against the fact sheet and then against the SEC filings, so \
precision matters more than polish.

How to use the fact sheet:
- The fact sheet is the only source of numbers. Each line is \
`fact_id | label | period | value`, and derived lines show the formula and \
input fact IDs.
- For every sentence, list in `fact_ids` each fact whose value (or whose \
difference or percent change with another cited fact) appears in that \
sentence. A sentence without numbers may cite nothing.
- The fact sheet gives dollars exactly and percentages to four decimals, so \
you always round once, straight from the value shown. Write amounts with a \
scale word ("$12.83 billion", "$845 million"), and ratios and percent \
changes to one decimal ("41.2%"). Round half away from zero and do not \
round in two steps: 14.6474% is "14.6%", not "14.7%". An automated check \
compares every figure against the cited value at the precision you wrote \
it, so fewer decimals is safer than a wrongly rounded extra one. Write margin moves in basis points \
("up 170 basis points") or percentage points ("1.7 percentage points"). \
Express direction with words (up/down, rose/fell), not minus signs.
- Do not write any other numerals. Spell out counts ("three of the nine \
peers"). Years and quarter labels such as 2024Q2 are fine.

What to write:
- Cover the headline, revenue, margins, operating expenses, profitability \
and peer/sector context, plus a data_caveats section when the anomaly log \
has entries for the periods discussed. Around eight to fourteen sentences.
- Explain movements only through arithmetic the facts support (for example, \
gross profit grew faster than revenue, so gross margin expanded). The facts \
contain no business drivers, so never attribute a change to products, \
demand, pricing, customers or management actions. Put driver questions the \
analyst should confirm with the business in `open_questions` (no numerals).
- Figures are GAAP as filed in XBRL. Non-GAAP measures, guidance, and \
forward-looking statements are out of scope: never state them, and if the \
analyst request asks for them, add a short explanation to `declined` \
instead.
- Values marked [derived by pipeline] were computed from filings rather \
than disclosed directly (for example, fourth quarter = annual minus \
nine-month year-to-date). Say so when the headline relies on one.
"""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": SECTIONS},
                    "text": {"type": "string"},
                    "fact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["section", "text", "fact_ids"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "declined": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentences", "open_questions", "declined"],
    "additionalProperties": False,
}


class DraftRefused(RuntimeError):
    pass


def build_user_message(sheet: FactSheet, analyst_request: str | None) -> str:
    anomalies = (
        "\n".join(f"- {a['period']} {a['metric']}: {a['rule']} ({a['action']})" for a in sheet.anomalies) or "- none"
    )
    request = analyst_request or "Standard quarterly variance commentary."
    return (
        f"Company: {sheet.company_name} ({sheet.ticker})\n"
        f"Focal quarter: {sheet.period} (calendar). Prior quarter: {sheet.prior_quarter}. "
        f"Year-ago quarter: {sheet.year_ago_quarter}.\n\n"
        f"<fact_sheet>\n{sheet.to_prompt_table()}\n</fact_sheet>\n\n"
        f"<anomaly_log>\n{anomalies}\n</anomaly_log>\n\n"
        f"<analyst_request>\n{request}\n</analyst_request>"
    )


class ClaudeDrafter:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", client=None):
        import anthropic

        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def draft(self, sheet: FactSheet, analyst_request: str | None = None) -> Draft:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(sheet, analyst_request)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
            # On a safety-classifier decline, let the API retry on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise DraftRefused(f"Model declined to draft (category={category}).")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Draft was truncated at max_tokens; rerun with a narrower request.")
        text = next(b.text for b in response.content if b.type == "text")
        return Draft.from_model_output(json.loads(text), drafter=response.model)


# ---------------------------------------------------------------------------
# Deterministic template
# ---------------------------------------------------------------------------


def fmt_money(value: float) -> str:
    v = abs(value)
    if v >= 1e9:
        return f"${v / 1e9:,.2f} billion"
    if v >= 1e6:
        return f"${v / 1e6:,.0f} million"
    return f"${v:,.0f}"


def fmt_pct(value: float) -> str:
    return f"{abs(value) * 100:.1f}%"


def fmt_bps(value: float) -> str:
    """Basis points for ordinary moves; percentage points once the move is
    big enough that "1389 basis points" is harder to read than "13.9
    percentage points". The verifier accepts either unit."""
    bps = abs(value) * 10000
    if bps >= 1000:
        return f"{bps / 100:.1f} percentage points"
    return f"{bps:.0f} basis points"


def _direction(value: float, up: str = "up", down: str = "down") -> str:
    return up if value >= 0 else down


class TemplateDrafter:
    """Rule-based commentary. Deliberately plain; its job is to prove the
    fact-sheet -> verifier -> review-queue contract end to end."""

    def draft(self, sheet: FactSheet, analyst_request: str | None = None) -> Draft:
        t, p = sheet.ticker, sheet.period
        f = lambda metric, entity=t: sheet.get(fact_id(entity, metric, p))  # noqa: E731
        out: list[Sentence] = []

        def add(section: str, text: str, *facts: Fact | None) -> None:
            out.append(Sentence(section, text, [x.id for x in facts if x is not None]))

        rev, rev_yoy, rev_qoq = f("revenue"), f("revenue_yoy_pct"), f("revenue_qoq_pct")
        if rev and rev_yoy:
            add(
                "headline",
                f"{sheet.company_name} reported {p} revenue of {fmt_money(rev.value)}, "
                f"{_direction(rev_yoy.value)} {fmt_pct(rev_yoy.value)} year over year.",
                rev,
                rev_yoy,
            )
        elif rev:
            add("headline", f"{sheet.company_name} reported {p} revenue of {fmt_money(rev.value)}.", rev)
        if rev and rev.source and rev.source.is_derived:
            add(
                "data_caveats",
                "Fourth-quarter revenue is not disclosed on its own in XBRL; the pipeline derived it as "
                "full-year revenue minus nine-month year-to-date revenue.",
            )
        if rev_qoq:
            add(
                "revenue",
                f"Sequentially, revenue was {_direction(rev_qoq.value)} {fmt_pct(rev_qoq.value)} "
                f"from {sheet.prior_quarter}.",
                rev_qoq,
            )

        gm, gm_yoy = f("gross_margin_pct"), f("gross_margin_pct_yoy_change")
        if gm and gm_yoy:
            verb = "expanded" if gm_yoy.value >= 0 else "contracted"
            add(
                "margins",
                f"Gross margin was {fmt_pct(gm.value)}, which {verb} by {fmt_bps(gm_yoy.value)} "
                f"from {sheet.year_ago_quarter}.",
                gm,
                gm_yoy,
            )
            gp_yoy = f("gross_profit_yoy_pct")
            if rev_yoy and gp_yoy:
                rev_move = f"{fmt_pct(rev_yoy.value)} {_direction(rev_yoy.value, 'increase', 'decrease')} in revenue"
                effect = "compressing" if gp_yoy.value < rev_yoy.value else "expanding"
                add(
                    "margins",
                    f"Gross profit was {_direction(gp_yoy.value)} {fmt_pct(gp_yoy.value)} year over year "
                    f"against a {rev_move}, {effect} gross margin.",
                    gp_yoy,
                    rev_yoy,
                )
        elif gm:
            add("margins", f"Gross margin was {fmt_pct(gm.value)}.", gm)

        rd, rd_int = f("research_and_development_expense"), f("rd_intensity_pct")
        if rd and rd_int:
            add(
                "operating_expenses",
                f"R&D expense was {fmt_money(rd.value)}, or {fmt_pct(rd_int.value)} of revenue.",
                rd,
                rd_int,
            )

        om, om_yoy = f("operating_margin_pct"), f("operating_margin_pct_yoy_change")
        oi = f("operating_income")
        if oi and om:
            word = "income" if oi.value >= 0 else "loss"
            add(
                "profitability",
                f"Operating {word} was {fmt_money(oi.value)}, an operating margin of "
                f"{'negative ' if om.value < 0 else ''}{fmt_pct(om.value)}.",
                oi,
                om,
            )
        if om_yoy:
            add(
                "profitability",
                f"Operating margin moved {_direction(om_yoy.value)} {fmt_bps(om_yoy.value)} year over year.",
                om_yoy,
            )
        ni = f("net_income")
        if ni:
            add("profitability", f"Net {'income' if ni.value >= 0 else 'loss'} was {fmt_money(ni.value)}.", ni)

        sector_gm = f("gross_margin_pct_median", "SECTOR")
        if gm and sector_gm:
            rel = "above" if gm.value >= sector_gm.value else "below"
            add(
                "peer_context",
                f"Gross margin sat {rel} the sector median of {fmt_pct(sector_gm.value)}.",
                gm,
                sector_gm,
            )
        sector_om = f("operating_margin_pct_median", "SECTOR")
        if om and sector_om:
            rel = "above" if om.value >= sector_om.value else "below"
            add(
                "peer_context",
                f"Operating margin was {rel} the sector median of {fmt_pct(sector_om.value)}.",
                om,
                sector_om,
            )

        peer_gms = [x for x in sheet.facts.values() if x.metric == "gross_margin_pct" and x.entity not in (t, "SECTOR")]
        if gm and peer_gms:
            above, n = sum(1 for x in peer_gms if x.value > gm.value), len(peer_gms)
            peers = f"{_count_word(n)} named peers with data this quarter"
            if above == n:
                text = f"All {peers} reported a higher gross margin."
            elif above == 0:
                text = f"None of the {peers} reported a higher gross margin."
            else:
                text = f"{_count_word(above).capitalize()} of the {peers} reported a higher gross margin."
            add("peer_context", text)

        # One caveat per (period, rule), listing the affected metrics.
        order = list(METRIC_NAMES)
        grouped: dict[tuple[str, str], set[str]] = {}
        for a in sheet.anomalies:
            grouped.setdefault((a["period"], a["rule"]), set()).add(a["metric"])
        for (period, rule), metrics in sorted(grouped.items()):
            ranked = sorted(metrics, key=lambda m: order.index(m) if m in order else len(order))
            listed = ", ".join(METRIC_NAMES.get(m, m) for m in ranked)
            add(
                "data_caveats",
                f"The anomaly log flags {CAVEATS.get(rule, rule.replace('_', ' '))} for {period} "
                f"({listed}); comparisons against that quarter should be confirmed by the reviewer.",
            )

        declined = []
        if analyst_request and any(
            k in analyst_request.lower() for k in ("non-gaap", "adjusted", "guidance", "outlook")
        ):
            declined.append("Non-GAAP measures, guidance and outlook are not in XBRL filings and are out of scope.")

        return Draft(
            sentences=out,
            open_questions=[
                "What business drivers explain the year-over-year revenue change?",
                "Were there one-time items in cost of revenue or operating expenses this quarter?",
            ],
            declined=declined,
            drafter="template-v1",
        )


METRIC_NAMES = {
    "revenue": "revenue",
    "cost_of_revenue": "cost of revenue",
    "gross_profit": "gross profit",
    "research_and_development_expense": "R&D",
    "sga_expense": "SG&A",
    "operating_income": "operating income",
    "net_income": "net income",
    "total_assets": "total assets",
    "total_liabilities": "total liabilities",
    "stockholders_equity": "equity",
}
CAVEATS = {
    "restatement_or_recast": "a restatement or recast (the panel uses the latest-filed values)",
    "concept_change": "a change in which XBRL concept the company reports, so values either side are not comparable",
    "scale_error": "a possible scale error",
    "ytd_consistency": "quarters that don't sum to the year-to-date total",
    "gross_profit_identity": "a broken gross-profit identity",
    "balance_sheet_identity": "a broken balance-sheet identity",
    "sign_anomaly": "an unexpected negative expense",
}

_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
]


def _count_word(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else "many"


def get_drafter(offline: bool, model: str = DEFAULT_MODEL) -> Drafter:
    if offline:
        return TemplateDrafter()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # The SDK can also pick up an `ant auth login` profile, so this is a
        # hint rather than a hard stop.
        print("note: ANTHROPIC_API_KEY is not set; relying on any configured `ant auth` profile.")
    return ClaudeDrafter(model=model)
