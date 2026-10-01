"""
Mutation testing for the verifier.

A verifier that never complains looks perfect until it ships a wrong number,
and one that complains about everything is ignored within a week. So the
eval measures both directions, starting from a draft that already passes:

  ERROR mutations   inject one specific defect — a figure nudged 2%, a
                    direction word flipped, a citation pointed at the wrong
                    fact, a tampered filing row. The verifier must report at
                    least one of the expected error codes. The share it
                    catches is RECALL.

  BENIGN mutations  rewrite the prose without changing its meaning — coarser
                    but still-correct rounding, a synonym, an extra sentence
                    carrying no numbers. The verifier must stay silent. The
                    share that stays clean is SPECIFICITY.

Each mutation returns None when it doesn't apply to the draft it's given
(no money figure to nudge, no direction word to flip), and the harness
records that as "not applicable" rather than as a pass.
"""

from __future__ import annotations

import copy
import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.agent.drafter import Draft, Sentence
from src.agent.facts import DIFFERENCE, PCT_CHANGE, RATIO, FactSheet
from src.agent.verifier import MONEY_RE, PERCENT_RE, POINTS_RE, SourceLedger

Case = tuple[Draft, FactSheet, SourceLedger | None]


@dataclass
class Mutation:
    name: str
    kind: str  # "error" or "benign"
    describe: str
    expected_codes: frozenset[str]  # for errors: at least one must be reported
    apply: Callable[[Draft, FactSheet, SourceLedger | None], Case | None]


# ---------------------------------------------------------------------------
# Text surgery helpers
# ---------------------------------------------------------------------------


def _rescale_match(text: str, pattern: re.Pattern, group: int, factor: float) -> str | None:
    """Multiplies the first number matched by `pattern` by `factor`, keeping
    the written precision, currency symbol and scale word intact."""
    m = pattern.search(text)
    if not m:
        return None
    written = m.group(group)
    decimals = len(written.split(".")[1]) if "." in written else 0
    value = float(written.replace(",", "")) * factor
    return text[: m.start(group)] + f"{value:,.{decimals}f}" + text[m.end(group) :]


def _first_index(draft: Draft, predicate: Callable[[Sentence], bool]) -> int | None:
    return next((i for i, s in enumerate(draft.sentences) if predicate(s)), None)


def _has(pattern: re.Pattern) -> Callable[[Sentence], bool]:
    return lambda s: bool(pattern.search(s.text)) and bool(s.fact_ids)


def _replace_sentence(draft: Draft, idx: int, text: str, fact_ids: list[str] | None = None) -> Draft:
    out = copy.deepcopy(draft)
    out.sentences[idx] = dataclasses.replace(
        out.sentences[idx], text=text, fact_ids=fact_ids if fact_ids is not None else out.sentences[idx].fact_ids
    )
    return out


def _append_sentence(draft: Draft, section: str, text: str, fact_ids: list[str] | None = None) -> Draft:
    out = copy.deepcopy(draft)
    out.sentences.append(Sentence(section, text, fact_ids or []))
    return out


def _change_facts(sheet: FactSheet, formulas: tuple[str, ...]) -> list[str]:
    return [f.id for f in sheet.facts.values() if f.formula in formulas]


# ---------------------------------------------------------------------------
# Error mutations
# ---------------------------------------------------------------------------


def _nudge_money(factor: float):
    def mutate(draft, sheet, ledger):
        idx = _first_index(draft, _has(MONEY_RE))
        if idx is None:
            return None
        text = _rescale_match(draft.sentences[idx].text, MONEY_RE, 2, factor)
        return (_replace_sentence(draft, idx, text), sheet, ledger) if text else None

    return mutate


def _nudge_percent(factor: float):
    def mutate(draft, sheet, ledger):
        idx = _first_index(draft, _has(PERCENT_RE))
        if idx is None:
            return None
        text = _rescale_match(draft.sentences[idx].text, PERCENT_RE, 2, factor)
        return (_replace_sentence(draft, idx, text), sheet, ledger) if text else None

    return mutate


def _nudge_basis_points(factor: float):
    def mutate(draft, sheet, ledger):
        idx = _first_index(draft, _has(POINTS_RE))
        if idx is None:
            return None
        text = _rescale_match(draft.sentences[idx].text, POINTS_RE, 1, factor)
        return (_replace_sentence(draft, idx, text), sheet, ledger) if text else None

    return mutate


UP_TO_DOWN = {"up": "down", "rose": "fell", "grew": "shrank", "higher": "lower", "expanded": "contracted"}


def _flip_direction(draft, sheet, ledger):
    """Flips an up/down word in a sentence whose figure is a change, so the
    arithmetic still matches but the prose points the wrong way."""
    change_ids = set(_change_facts(sheet, (PCT_CHANGE, DIFFERENCE)))
    for idx, s in enumerate(draft.sentences):
        if not change_ids.intersection(s.fact_ids):
            continue
        for word, opposite in UP_TO_DOWN.items():
            pattern = re.compile(rf"\b{word}\b", re.I)
            if pattern.search(s.text):
                return _replace_sentence(draft, idx, pattern.sub(opposite, s.text, count=1)), sheet, ledger
    return None


def _drop_citations(draft, sheet, ledger):
    idx = _first_index(draft, _has(MONEY_RE))
    if idx is None:
        return None
    return _replace_sentence(draft, idx, draft.sentences[idx].text, fact_ids=[]), sheet, ledger


def _fake_citation(draft, sheet, ledger):
    idx = _first_index(draft, _has(MONEY_RE))
    if idx is None:
        return None
    fake = f"{sheet.ticker}.revenue.1999Q1"
    return _replace_sentence(draft, idx, draft.sentences[idx].text, fact_ids=[fake]), sheet, ledger


def _swap_citation(draft, sheet, ledger):
    """Repoints a money sentence at a different real fact with a different
    value — the citation exists, but it isn't the number that was written."""
    idx = _first_index(draft, _has(MONEY_RE))
    if idx is None:
        return None
    cited = set(draft.sentences[idx].fact_ids)
    written = {sheet.get(f).value for f in cited if sheet.get(f)}
    other = next(
        (f.id for f in sheet.facts.values() if f.unit != RATIO and f.id not in cited and f.value not in written),
        None,
    )
    if other is None:
        return None
    return _replace_sentence(draft, idx, draft.sentences[idx].text, fact_ids=[other]), sheet, ledger


def _tamper_filing(draft, sheet, ledger):
    """Changes a value in the 'as filed' ledger so the fact sheet no longer
    agrees with the filing it cites — i.e. a pipeline bug, not an LLM one."""
    if ledger is None:
        return None
    cited = {fid for s in draft.sentences for fid in s.fact_ids}
    for fid in sorted(cited):
        fact = sheet.get(fid)
        if fact is None or fact.source is None:
            continue
        src = fact.source
        adsh, tag = src.adsh.split("|")[0], src.tag.split("|")[0]
        qtrs = 4 if src.derivation == "annual_minus_ytd9" else src.qtrs
        filed = ledger.values(adsh, tag, qtrs).get(src.ddate)
        if filed is None:
            continue
        return draft, sheet, ledger.with_override(adsh, tag, src.ddate, qtrs, filed * 1.05)
    return None


def _tamper_derived_ratio(draft, sheet, ledger):
    """Breaks a ratio's value AND the prose quoting it by the same factor, so
    the two agree with each other and only a recompute from the ratio's
    inputs can tell that it's wrong."""
    for idx, s in enumerate(draft.sentences):
        if not PERCENT_RE.search(s.text):
            continue
        target = next((f for f in (sheet.get(i) for i in s.fact_ids) if f and f.formula), None)
        if target is None:
            continue
        text = _rescale_match(s.text, PERCENT_RE, 2, 1.10)
        if text is None:
            continue
        mutated_sheet = copy.deepcopy(sheet)
        mutated_sheet.facts[target.id] = dataclasses.replace(target, value=target.value * 1.10)
        return _replace_sentence(draft, idx, text), mutated_sheet, ledger
    return None


def _add_non_gaap(draft, sheet, ledger):
    return _append_sentence(draft, "margins", "Non-GAAP gross margin was stronger than the reported figure."), sheet, ledger


def _add_forward_looking(draft, sheet, ledger):
    return _append_sentence(draft, "headline", "Management expects revenue to grow next quarter."), sheet, ledger


def _add_bare_numeral(draft, sheet, ledger):
    return _append_sentence(draft, "revenue", "Revenue has now grown for 3 straight quarters."), sheet, ledger


def _add_number_to_open_question(draft, sheet, ledger):
    out = copy.deepcopy(draft)
    out.open_questions.append("What drove the 12.5% move in revenue?")
    return out, sheet, ledger


# ---------------------------------------------------------------------------
# Benign mutations — these must NOT be flagged
# ---------------------------------------------------------------------------


def _coarsen_money(draft, sheet, ledger):
    """Rounds a figure to fewer decimals, still correct at that precision:
    $14.367B written as "$14.4 billion" is a true statement."""
    idx = _first_index(draft, lambda s: bool(s.fact_ids) and bool(MONEY_RE.search(s.text)))
    if idx is None:
        return None
    s = draft.sentences[idx]
    m = MONEY_RE.search(s.text)
    written = m.group(2)
    if "." not in written:
        return None
    value = float(written.replace(",", ""))
    coarse = f"{value:,.{max(0, len(written.split('.')[1]) - 1)}f}"
    return _replace_sentence(draft, idx, s.text[: m.start(2)] + coarse + s.text[m.end(2) :]), sheet, ledger


def _synonym_direction(draft, sheet, ledger):
    for idx, s in enumerate(draft.sentences):
        if re.search(r"\bup\b", s.text, re.I):
            return _replace_sentence(draft, idx, re.sub(r"\bup\b", "higher by", s.text, count=1)), sheet, ledger
        if re.search(r"\bdown\b", s.text, re.I):
            return _replace_sentence(draft, idx, re.sub(r"\bdown\b", "lower by", s.text, count=1)), sheet, ledger
    return None


def _add_numberless_sentence(draft, sheet, ledger):
    return _append_sentence(draft, "revenue", "The quarter's mix across the product portfolio is not disclosed in XBRL."), sheet, ledger


def _spell_out_a_count(draft, sheet, ledger):
    return _append_sentence(draft, "peer_context", "Two of the named peers do not report a comparable figure."), sheet, ledger


def _reword_keeping_numbers(draft, sheet, ledger):
    idx = _first_index(draft, _has(MONEY_RE))
    if idx is None:
        return None
    s = draft.sentences[idx]
    return _replace_sentence(draft, idx, f"On a reported basis, {s.text[0].lower() + s.text[1:]}"), sheet, ledger


ERROR_MUTATIONS = [
    Mutation("money_off_by_2pct", "error", "a dollar figure inflated by 2%", frozenset({"number_mismatch"}), _nudge_money(1.02)),
    Mutation("money_off_by_1000x", "error", "a dollar figure reported in the wrong scale", frozenset({"number_mismatch"}), _nudge_money(1000)),
    Mutation("money_last_digit", "error", "a dollar figure wrong in its last written digit", frozenset({"number_mismatch"}), _nudge_money(1.001)),
    Mutation("percent_off_by_10pct", "error", "a percentage inflated by a tenth", frozenset({"number_mismatch"}), _nudge_percent(1.10)),
    Mutation("basis_points_off", "error", "a margin move overstated by half", frozenset({"number_mismatch"}), _nudge_basis_points(1.5)),
    Mutation("direction_flipped", "error", "a correct figure described in the wrong direction", frozenset({"direction_mismatch"}), _flip_direction),
    Mutation("citations_dropped", "error", "numbers with no cited source", frozenset({"uncited_number"}), _drop_citations),
    Mutation("citation_invented", "error", "a citation to a fact that doesn't exist", frozenset({"unknown_fact_id"}), _fake_citation),
    Mutation("citation_swapped", "error", "a real citation that isn't the written number", frozenset({"number_mismatch"}), _swap_citation),
    Mutation("filing_tampered", "error", "a fact sheet value that disagrees with the filing", frozenset({"source_mismatch"}), _tamper_filing),
    Mutation("ratio_internally_wrong", "error", "a ratio that doesn't equal its own inputs", frozenset({"recompute_mismatch"}), _tamper_derived_ratio),
    Mutation("non_gaap_added", "error", "a non-GAAP measure", frozenset({"non_gaap"}), _add_non_gaap),
    Mutation("forward_looking_added", "error", "a forward-looking statement", frozenset({"forward_looking"}), _add_forward_looking),
    Mutation("bare_numeral_added", "error", "an unverifiable bare numeral", frozenset({"unverifiable_numeral"}), _add_bare_numeral),
    Mutation("number_in_open_question", "error", "a number smuggled into an open question", frozenset({"number_in_open_question"}), _add_number_to_open_question),
]

BENIGN_MUTATIONS = [
    Mutation("rounded_coarser", "benign", "a figure rounded to fewer decimals", frozenset(), _coarsen_money),
    Mutation("direction_synonym", "benign", "an up/down word swapped for a synonym", frozenset(), _synonym_direction),
    Mutation("numberless_sentence_added", "benign", "an added sentence with no figures", frozenset(), _add_numberless_sentence),
    Mutation("count_spelled_out", "benign", "a count written as a word", frozenset(), _spell_out_a_count),
    Mutation("sentence_reworded", "benign", "a sentence reworded around the same figures", frozenset(), _reword_keeping_numbers),
]

ALL_MUTATIONS = ERROR_MUTATIONS + BENIGN_MUTATIONS
