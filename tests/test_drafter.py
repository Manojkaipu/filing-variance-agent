import json
from types import SimpleNamespace

import pytest

from src.agent.drafter import DRAFT_SCHEMA, ClaudeDrafter, DraftRefused, TemplateDrafter, build_user_message
from src.agent.facts import FactSheet
from src.agent.verifier import verify
from tests.factories import make_ledger, make_sheet


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(payload=None, stop_reason="end_turn", stop_details=None):
    content = [SimpleNamespace(type="text", text=json.dumps(payload))] if payload is not None else []
    response = SimpleNamespace(
        stop_reason=stop_reason, stop_details=stop_details, content=content, model="claude-opus-5-5"
    )
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


MODEL_OUTPUT = {
    "sentences": [
        {
            "section": "headline",
            "text": "Acme Corp reported 2024Q4 revenue of $11.0 billion, up 10.0% year over year.",
            "fact_ids": ["ACME.revenue.2024Q4", "ACME.revenue_yoy_pct.2024Q4"],
        },
        {
            "section": "margins",
            "text": "Gross margin expanded 300 basis points to 43.0%.",
            "fact_ids": ["ACME.gross_margin_pct.2024Q4", "ACME.gross_margin_pct_yoy_change.2024Q4"],
        },
    ],
    "open_questions": ["Which product lines drove the revenue increase?"],
    "declined": [],
}


def test_claude_drafter_sends_fact_sheet_and_schema_and_parses_the_draft():
    client, messages = fake_client(MODEL_OUTPUT)
    draft = ClaudeDrafter(client=client).draft(make_sheet())

    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": DRAFT_SCHEMA}
    assert call["fallbacks"] == "default"
    prompt = call["messages"][0]["content"]
    # Exact dollars and 4-decimal percents in the prompt, so the model never
    # rounds a value that was already rounded for display.
    assert "ACME.revenue.2024Q4 | Revenue | 2024Q4 | $11,000,000,000" in prompt
    assert "ACME.gross_margin_pct.2024Q4 | Gross margin | 2024Q4 | 43.0000%" in prompt
    assert "restatement_or_recast" in prompt  # anomaly log is shown to the model

    assert len(draft.sentences) == 2 and draft.drafter == "claude-opus-5-5"
    assert verify(draft, make_sheet(), make_ledger()).passed


def test_model_hallucinated_number_is_caught_downstream():
    bad = json.loads(json.dumps(MODEL_OUTPUT))
    bad["sentences"][0]["text"] = "Acme Corp reported 2024Q4 revenue of $11.4 billion, up 10.0% year over year."
    client, _ = fake_client(bad)
    report = verify(ClaudeDrafter(client=client).draft(make_sheet()), make_sheet(), make_ledger())
    assert not report.passed


def test_refusal_is_surfaced_not_parsed():
    client, _ = fake_client(None, stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(DraftRefused):
        ClaudeDrafter(client=client).draft(make_sheet())


def test_user_message_carries_the_analyst_request():
    msg = build_user_message(make_sheet(), "Explain non-GAAP gross margin.")
    assert "<analyst_request>\nExplain non-GAAP gross margin.\n</analyst_request>" in msg


def test_template_declines_non_gaap_requests():
    draft = TemplateDrafter().draft(make_sheet(), "Include adjusted EBITDA and non-GAAP margin")
    assert draft.declined


def test_template_groups_anomalies_into_one_caveat_per_period_and_rule():
    anomalies = [
        dict(rule="restatement_or_recast", cik="1", metric=m, period="2023Q4", detail="", action="kept")
        for m in ("revenue", "net_income", "sga_expense")
    ]
    draft = TemplateDrafter().draft(make_sheet(anomalies))
    caveats = [s.text for s in draft.sentences if s.section == "data_caveats" and "anomaly log" in s.text]
    assert len(caveats) == 1
    assert "(revenue, SG&A, net income)" in caveats[0]


def test_prompt_precision_lets_the_model_round_once():
    from src.agent.facts import RATIO, format_value

    # The real failure: 14.6474% shown as "14.65%" invites 14.7%, but the
    # correct one-step rounding is 14.6%.
    assert format_value(0.146474, RATIO) == "14.65%"  # human-facing
    assert format_value(0.146474, RATIO, precise=True) == "14.6474%"  # prompt
    assert format_value(0.388496, RATIO, precise=True) == "38.8496%"
    assert format_value(11_000_000_000.0, "USD", precise=True) == "$11,000,000,000"


def test_margin_moves_switch_to_percentage_points_when_large():
    from src.agent.drafter import fmt_bps

    assert fmt_bps(0.017) == "170 basis points"
    assert fmt_bps(-0.0045) == "45 basis points"
    # Intel's 2024Q4 operating margin move: "1389 basis points" reads badly.
    assert fmt_bps(-0.1389) == "13.9 percentage points"


def test_fact_sheet_json_round_trip():
    sheet = make_sheet()
    restored = FactSheet.from_json(sheet.to_json())
    assert restored.facts == sheet.facts
    assert restored.anomalies == sheet.anomalies
