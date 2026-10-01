"""
Two measurements, reported as numbers rather than assertions.

1. `run_verifier_eval` — does the verifier actually catch wrong numbers?
   Starts from a draft that passes, applies each mutation in
   src/eval/mutations.py, and reports recall (injected defects caught) and
   specificity (valid rewrites left alone).

2. `run_drafter_eval` — how often does a real drafter clear verification
   unaided? Runs one draft per company-quarter and reports the pass rate
   with a breakdown of what failed. Costs money when the drafter is Claude,
   so the caller passes the case list explicitly.

Both return plain dicts so the CLI can print them and CI can diff them.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.agent.drafter import Drafter
from src.agent.facts import FactSheet
from src.agent.verifier import ERROR, WARNING, SourceLedger, VerificationReport, verify
from src.eval.mutations import ALL_MUTATIONS, Mutation

CAUGHT, MISSED, NOT_APPLICABLE, FALSE_POSITIVE, CLEAN = "caught", "missed", "n/a", "false_positive", "clean"


@dataclass
class MutationOutcome:
    mutation: str
    kind: str
    describe: str
    status: str
    reported_codes: tuple[str, ...] = ()


def _error_codes(report: VerificationReport) -> tuple[str, ...]:
    return tuple(sorted({i.code for i in report.issues if i.level == ERROR}))


def evaluate_mutation(mutation: Mutation, draft, sheet: FactSheet, ledger: SourceLedger | None) -> MutationOutcome:
    mutated = mutation.apply(draft, sheet, ledger)
    if mutated is None:
        return MutationOutcome(mutation.name, mutation.kind, mutation.describe, NOT_APPLICABLE)

    report = verify(*mutated)
    codes = _error_codes(report)
    if mutation.kind == "error":
        status = CAUGHT if mutation.expected_codes.intersection(codes) else MISSED
    else:
        status = CLEAN if not codes else FALSE_POSITIVE
    return MutationOutcome(mutation.name, mutation.kind, mutation.describe, status, codes)


def run_verifier_eval(draft, sheet: FactSheet, ledger: SourceLedger | None) -> dict:
    """The baseline draft must pass before any mutation means anything: if it
    already fails, 'the verifier caught the injected error' proves nothing."""
    baseline = verify(draft, sheet, ledger)
    outcomes = [evaluate_mutation(m, draft, sheet, ledger) for m in ALL_MUTATIONS]

    errors = [o for o in outcomes if o.kind == "error" and o.status != NOT_APPLICABLE]
    benign = [o for o in outcomes if o.kind == "benign" and o.status != NOT_APPLICABLE]
    caught = [o for o in errors if o.status == CAUGHT]
    clean = [o for o in benign if o.status == CLEAN]

    return {
        "baseline_passed": baseline.passed,
        "baseline_numbers_checked": baseline.numbers_checked,
        "baseline_facts_source_checked": baseline.facts_source_checked,
        "baseline_errors": list(_error_codes(baseline)),
        "recall": {
            "caught": len(caught),
            "applicable": len(errors),
            "missed": [o.mutation for o in errors if o.status == MISSED],
        },
        "specificity": {
            "clean": len(clean),
            "applicable": len(benign),
            "false_positives": [
                {"mutation": o.mutation, "codes": list(o.reported_codes)} for o in benign if o.status == FALSE_POSITIVE
            ],
        },
        "skipped": [o.mutation for o in outcomes if o.status == NOT_APPLICABLE],
        "outcomes": [vars(o) | {"reported_codes": list(o.reported_codes)} for o in outcomes],
    }


def run_drafter_eval(
    drafter: Drafter, cases: list[tuple[FactSheet, SourceLedger | None]], progress: bool = True
) -> dict:
    """One draft per case. A drafter that raises (a refusal, a truncated
    response) is recorded as an error rather than silently dropped.

    Each case is reported as it finishes: a 20-case run against the API
    takes minutes, and silence is indistinguishable from a hang."""
    results, error_codes, warning_codes = [], Counter(), Counter()
    for n, (sheet, ledger) in enumerate(cases, 1):
        started = time.perf_counter()
        label = f"{sheet.ticker} {sheet.period}"
        try:
            draft = drafter.draft(sheet)
        except Exception as e:  # refusal, truncation, transport failure
            results.append({"case": label, "status": "drafter_error", "detail": f"{type(e).__name__}: {e}"})
            if progress:
                print(f"  [{n}/{len(cases)}] {label}: drafter error — {type(e).__name__}", flush=True)
            continue

        report = verify(draft, sheet, ledger)
        error_codes.update(i.code for i in report.issues if i.level == ERROR)
        warning_codes.update(i.code for i in report.issues if i.level == WARNING)
        if progress:
            codes = ", ".join(_error_codes(report)) or "clean"
            print(
                f"  [{n}/{len(cases)}] {label}: {'passed' if report.passed else 'FAILED'}"
                f" — {report.numbers_checked} numbers, {codes}",
                flush=True,
            )
        results.append(
            {
                "case": label,
                "status": "passed" if report.passed else "failed",
                "drafter": draft.drafter,
                "sentences": len(draft.sentences),
                "numbers_checked": report.numbers_checked,
                "facts_source_checked": report.facts_source_checked,
                "errors": list(_error_codes(report)),
                "warnings": sorted({i.code for i in report.issues if i.level == WARNING}),
                # The messages, not just the codes: "number_mismatch x4" says
                # nothing about whether the model misread a fact or rounded
                # badly, and that is the whole question when deciding what to
                # fix in the prompt.
                "error_detail": [
                    {"code": i.code, "sentence": i.sentence_index, "message": i.message}
                    for i in report.issues
                    if i.level == ERROR
                ],
                "sentences_text": [s.text for s in draft.sentences],
                "seconds": round(time.perf_counter() - started, 1),
            }
        )

    passed = sum(r["status"] == "passed" for r in results)
    return {
        "cases": len(results),
        "passed": passed,
        "failed": sum(r["status"] == "failed" for r in results),
        "drafter_errors": sum(r["status"] == "drafter_error" for r in results),
        "pass_rate": round(passed / len(results), 3) if results else None,
        "numbers_checked": sum(r.get("numbers_checked", 0) for r in results),
        "error_codes": dict(error_codes.most_common()),
        "warning_codes": dict(warning_codes.most_common()),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_STATUS_MARK = {
    CAUGHT: "caught",
    MISSED: "MISSED",
    CLEAN: "clean",
    FALSE_POSITIVE: "FALSE POSITIVE",
    NOT_APPLICABLE: "n/a",
}


def format_verifier_report(result: dict) -> str:
    recall, spec = result["recall"], result["specificity"]
    lines = [
        "Verifier eval",
        "  baseline draft: "
        + ("passes" if result["baseline_passed"] else "FAILS — " + ", ".join(result["baseline_errors"]))
        + f" ({result['baseline_numbers_checked']} numbers,"
        + f" {result['baseline_facts_source_checked']} facts re-derived)",
        "",
        "  Injected defects (must be caught):",
    ]
    for o in result["outcomes"]:
        if o["kind"] != "error":
            continue
        lines.append(f"    {_STATUS_MARK[o['status']]:<14} {o['mutation']:<28} {o['describe']}")
    lines += ["", "  Valid rewrites (must be left alone):"]
    for o in result["outcomes"]:
        if o["kind"] != "benign":
            continue
        codes = f"  [{', '.join(o['reported_codes'])}]" if o["reported_codes"] else ""
        lines.append(f"    {_STATUS_MARK[o['status']]:<14} {o['mutation']:<28} {o['describe']}{codes}")

    lines += [
        "",
        f"  recall:      {recall['caught']}/{recall['applicable']} injected defects caught",
        f"  specificity: {spec['clean']}/{spec['applicable']} valid rewrites passed untouched",
    ]
    if result["skipped"]:
        lines.append(f"  not applicable to this draft: {', '.join(result['skipped'])}")
    return "\n".join(lines)


def format_drafter_report(result: dict) -> str:
    lines = [
        "Drafter eval",
        f"  cases: {result['cases']} company-quarters, {result['numbers_checked']} numbers verified",
        f"  passed verification unaided: {result['passed']}/{result['cases']}"
        + (f" ({result['pass_rate'] * 100:.0f}%)" if result["pass_rate"] is not None else ""),
    ]
    if result["drafter_errors"]:
        lines.append(f"  drafter errors (refusal/truncation): {result['drafter_errors']}")
    if result["error_codes"]:
        lines.append("  blocking issues: " + ", ".join(f"{c} x{n}" for c, n in result["error_codes"].items()))
    if result["warning_codes"]:
        lines.append("  reviewer warnings: " + ", ".join(f"{c} x{n}" for c, n in result["warning_codes"].items()))
    lines.append("")
    for r in result["results"]:
        detail = r.get("detail") or ", ".join(r.get("errors") or []) or "-"
        lines.append(f"    {r['status']:<14} {r['case']:<16} {detail}")
    return "\n".join(lines)


def write_report(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
