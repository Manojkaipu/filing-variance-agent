"""
The eval harness is itself under test: if a mutation silently stops applying,
recall would climb toward 100% for the wrong reason.
"""

import pytest

from src.agent.drafter import Draft, Sentence, TemplateDrafter
from src.eval.harness import (
    CAUGHT,
    CLEAN,
    NOT_APPLICABLE,
    evaluate_mutation,
    format_drafter_report,
    format_verifier_report,
    run_drafter_eval,
    run_verifier_eval,
)
from src.eval.mutations import ALL_MUTATIONS, BENIGN_MUTATIONS, ERROR_MUTATIONS
from tests.factories import make_ledger, make_sheet


@pytest.fixture(scope="module")
def baseline():
    sheet = make_sheet()
    return TemplateDrafter().draft(sheet), sheet, make_ledger()


@pytest.fixture(scope="module")
def result(baseline):
    return run_verifier_eval(*baseline)


def test_baseline_draft_passes_so_mutations_mean_something(result):
    assert result["baseline_passed"], result["baseline_errors"]


def test_every_mutation_applies_to_the_baseline_draft(result):
    # A skipped mutation measures nothing; it must not be mistaken for a pass.
    assert result["skipped"] == []


def test_verifier_catches_every_injected_defect(result):
    assert result["recall"]["missed"] == []
    assert result["recall"]["caught"] == result["recall"]["applicable"] == len(ERROR_MUTATIONS)


def test_verifier_leaves_valid_rewrites_alone(result):
    assert result["specificity"]["false_positives"] == []
    assert result["specificity"]["clean"] == result["specificity"]["applicable"] == len(BENIGN_MUTATIONS)


@pytest.mark.parametrize("mutation", ERROR_MUTATIONS, ids=lambda m: m.name)
def test_each_defect_is_caught_by_its_own_expected_code(mutation, baseline):
    # Not just "some error fired" — the error the mutation was designed to
    # trigger. A number_mismatch standing in for a missed direction flip
    # would otherwise look like a pass.
    outcome = evaluate_mutation(mutation, *baseline)
    assert outcome.status == CAUGHT, f"{mutation.name}: reported {outcome.reported_codes}"
    assert mutation.expected_codes.intersection(outcome.reported_codes)


@pytest.mark.parametrize("mutation", BENIGN_MUTATIONS, ids=lambda m: m.name)
def test_each_valid_rewrite_stays_clean(mutation, baseline):
    outcome = evaluate_mutation(mutation, *baseline)
    assert outcome.status == CLEAN, f"{mutation.name}: reported {outcome.reported_codes}"


def test_mutation_that_does_not_apply_is_recorded_as_such(baseline):
    _, sheet, ledger = baseline
    empty = Draft([Sentence("revenue", "Revenue was unchanged.", [])], drafter="test")
    money_mutation = next(m for m in ALL_MUTATIONS if m.name == "money_off_by_2pct")
    assert evaluate_mutation(money_mutation, empty, sheet, ledger).status == NOT_APPLICABLE


def test_drafter_eval_reports_pass_rate_and_survives_a_failing_drafter(baseline):
    _, sheet, ledger = baseline

    class Boom:
        def draft(self, sheet, analyst_request=None):
            raise RuntimeError("refused")

    good = run_drafter_eval(TemplateDrafter(), [(sheet, ledger)], progress=False)
    assert good["pass_rate"] == 1.0 and good["passed"] == 1

    # A draft that never came back says nothing about accuracy, so it is
    # excluded from the denominator and reported on its own. Folding it in
    # would let an outage masquerade as a quality regression (and a billing
    # failure mid-run did exactly that on the first real run).
    bad = run_drafter_eval(Boom(), [(sheet, ledger)], progress=False)
    assert bad["drafter_errors"] == 1
    assert bad["completed"] == 0 and bad["pass_rate"] is None
    assert "refused" in bad["results"][0]["detail"]

    mixed = run_drafter_eval(TemplateDrafter(), [(sheet, ledger)], progress=False)
    mixed_report = format_drafter_report({**mixed, "cases": 2, "drafter_errors": 1})
    assert "1/1 drafts that completed" in mixed_report
    assert "did not complete" in mixed_report


def test_reports_render_without_error(result, baseline):
    _, sheet, ledger = baseline
    assert "recall:" in format_verifier_report(result)
    assert "passed verification unaided" in format_drafter_report(
        run_drafter_eval(TemplateDrafter(), [(sheet, ledger)])
    )
