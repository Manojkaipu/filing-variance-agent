import dataclasses

import pytest

from src.agent.drafter import Draft, Sentence, TemplateDrafter
from src.agent.verifier import ERROR, WARNING, extract_quantities, verify
from tests.factories import A24, Q324, make_ledger, make_sheet

REV = "ACME.revenue.2024Q4"
REV_PRIOR_YEAR = "ACME.revenue.2023Q4"
REV_YOY = "ACME.revenue_yoy_pct.2024Q4"
GM = "ACME.gross_margin_pct.2024Q4"
GM_PRIOR_YEAR = "ACME.gross_margin_pct.2023Q4"
GM_YOY = "ACME.gross_margin_pct_yoy_change.2024Q4"


def check(text, fact_ids, ledger=None, open_questions=()):
    draft = Draft([Sentence("revenue", text, list(fact_ids))], open_questions=list(open_questions))
    return verify(draft, make_sheet(), ledger if ledger is not None else make_ledger())


def codes(report, level=ERROR):
    return {i.code for i in report.issues if i.level == level}


# --- number extraction --------------------------------------------------------


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ("$845 million", "money", 845e6),
        ("$12.83 billion", "money", 12.83e9),
        ("$1.2B", "money", 1.2e9),
        ("$3,400", "money", 3400),
        ("41.2%", "percent", 0.412),
        ("170 basis points", "points", 0.017),
        ("1.7 percentage points", "points", 0.017),
        ("45 bps", "points", 0.0045),
    ],
)
def test_extracts_financial_quantities(text, kind, value):
    quantities, strays = extract_quantities(f"It was {text} in 2024Q4.")
    assert strays == []
    assert [(q.kind, pytest.approx(q.value)) for q in quantities] == [(kind, pytest.approx(value))]


def test_digits_in_a_company_name_are_not_treated_as_figures():
    # 3M and 7-Eleven are real filers; the subject of the sentence must not
    # be flagged as an unverifiable figure.
    quantities, strays = extract_quantities("3M Company reported a lower margin.", exempt=("3M Company",))
    assert quantities == [] and strays == []
    # Without the exemption the same digits are correctly suspicious.
    assert extract_quantities("3M Company reported a lower margin.")[1] == ["3"]


def test_ordinals_naming_a_percentile_are_not_figures():
    # Claude wrote "above the 25th percentile of 35.6%" on the first real run;
    # the 25 names which percentile, and the 35.6% is the checked value.
    quantities, strays = extract_quantities("Margin sat above the 25th percentile of 35.6%.")
    assert strays == []
    assert [q.kind for q in quantities] == ["percent"]
    # A bare count still has to be spelled out.
    assert extract_quantities("Revenue grew for 3 straight quarters.")[1] == ["3"]


def test_years_quarters_and_form_names_are_not_numbers():
    quantities, strays = extract_quantities("In 2024Q4 (Q4 2024, fiscal 2025), per the 10-K and 10-Q/A filings.")
    assert quantities == [] and strays == []


# --- check 1: prose vs. cited facts ------------------------------------------


def test_template_draft_verifies_cleanly():
    sheet = make_sheet()
    report = verify(TemplateDrafter().draft(sheet), sheet, make_ledger())
    assert report.passed, report.issues
    assert report.numbers_checked >= 10
    assert report.facts_source_checked >= 5


def test_correct_numbers_pass():
    report = check("Revenue was $11.0 billion, up 10.0% year over year.", [REV, REV_YOY])
    assert report.passed, report.issues


def test_coarser_rounding_is_accepted_but_wrong_rounding_is_not():
    assert check("Revenue was $11 billion.", [REV]).passed
    assert "number_mismatch" in codes(check("Revenue was $11.04 billion.", [REV]))


def test_wrong_number_is_caught():
    report = check("Revenue was $11.50 billion.", [REV])
    assert "number_mismatch" in codes(report)
    assert not report.passed


def test_percent_change_between_two_cited_levels_is_recomputed():
    assert check("Revenue rose 10.0% from 2023Q4.", [REV, REV_PRIOR_YEAR]).passed
    assert "number_mismatch" in codes(check("Revenue rose 12.0% from 2023Q4.", [REV, REV_PRIOR_YEAR]))


def test_margin_move_in_basis_points_and_percentage_points():
    assert check("Gross margin expanded 300 basis points.", [GM_YOY]).passed
    assert check("Gross margin expanded 3.0 percentage points.", [GM, GM_PRIOR_YEAR]).passed
    assert "number_mismatch" in codes(check("Gross margin expanded 310 basis points.", [GM_YOY]))


def test_wrong_direction_is_caught():
    report = check("Revenue fell 10.0% year over year.", [REV_YOY])
    assert "direction_mismatch" in codes(report)


def test_number_without_citation_is_caught():
    assert "uncited_number" in codes(check("Revenue was $11.0 billion.", []))


def test_unknown_fact_id_is_caught():
    assert "unknown_fact_id" in codes(check("Revenue was $11.0 billion.", ["ACME.revenue.2031Q1"]))


def test_unformatted_numeral_is_caught():
    report = check("Revenue grew for 3 straight quarters.", [])
    assert "unverifiable_numeral" in codes(report)


def test_numbers_in_open_questions_are_rejected():
    report = check("Revenue grew.", [], open_questions=["Why did revenue rise 10%?"])
    assert "number_in_open_question" in codes(report)


# --- policy -------------------------------------------------------------------


def test_non_gaap_and_forward_looking_language_fail():
    assert "non_gaap" in codes(check("Non-GAAP gross margin improved.", []))
    assert "forward_looking" in codes(check("Management expects revenue to grow.", []))


def test_causal_claims_are_warnings_for_the_reviewer():
    report = check("Revenue was up 10.0% year over year, driven by data center demand.", [REV_YOY])
    assert report.passed
    assert "causal_claim" in codes(report, WARNING)


# --- check 2: fact sheet vs. filings -------------------------------------------


def test_fact_that_disagrees_with_the_filing_is_caught():
    # The 10-Q as filed says Q3 revenue was 10,400M; the fact sheet says 10,500M.
    ledger = make_ledger({(Q324, "Revenues", "2024-09-30", 1): 10_400e6})
    report = check("Revenue was $10.5 billion in 2024Q3.", ["ACME.revenue.2024Q3"], ledger)
    assert "source_mismatch" in codes(report)


def test_derived_q4_is_re_derived_from_annual_and_ytd_filings():
    assert check("Revenue was $11.0 billion.", [REV]).facts_source_checked == 1
    # Tamper with the annual figure in the 10-K: the re-derived Q4 no longer matches.
    ledger = make_ledger({(A24, "Revenues", "2024-12-31", 4): 42_000e6})
    assert "source_mismatch" in codes(check("Revenue was $11.0 billion.", [REV], ledger))


def test_derived_ratio_is_recomputed_from_inputs():
    sheet = make_sheet()
    sheet.facts[GM] = dataclasses.replace(sheet.facts[GM], value=0.45)  # inputs say 43.0%
    draft = Draft([Sentence("margins", "Gross margin was 45.0%.", [GM])])
    report = verify(draft, sheet, make_ledger())
    assert "recompute_mismatch" in codes(report)


def test_citing_a_ratio_source_checks_its_inputs_too():
    report = check("Gross margin was 43.0%.", [GM])
    assert report.passed
    # gross margin -> derived gross profit -> revenue and cost of revenue (both derived Q4s).
    assert report.facts_source_checked == 2


def test_missing_ledger_is_a_warning_not_a_silent_pass():
    draft = Draft([Sentence("revenue", "Revenue was $11.0 billion.", [REV])])
    report = verify(draft, make_sheet(), ledger=None)
    assert "source_not_checked" in codes(report, WARNING)
