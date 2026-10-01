import pytest

from src.agent.drafter import Draft, Sentence, TemplateDrafter
from src.agent.verifier import verify
from src.review.queue import APPROVED, BLOCKED, PENDING, REJECTED, ReviewError, ReviewQueue
from tests.factories import make_ledger, make_sheet


@pytest.fixture
def queue(tmp_path):
    return ReviewQueue(tmp_path / "queue.db")


def submit(queue, draft=None):
    sheet = make_sheet()
    draft = draft or TemplateDrafter().draft(sheet)
    return queue.submit(sheet, draft, verify(draft, sheet, make_ledger()))


def bad_draft():
    return Draft([Sentence("headline", "Revenue was $99.0 billion.", ["ACME.revenue.2024Q4"])], drafter="test")


def test_verified_draft_waits_for_review_and_exports_with_filing_links(queue):
    draft_id = submit(queue)
    assert queue.get(draft_id).status == PENDING

    queue.approve(draft_id, reviewer="A. Analyst", note="Looks right.")
    item = queue.get(draft_id)
    assert item.status == APPROVED and item.reviewer == "A. Analyst" and not item.override

    md = queue.export_markdown(draft_id)
    assert md.startswith("# Acme Corp (ACME) — 2024Q4 variance commentary")
    assert "## Headline" in md and "[^1]" in md
    # Footnotes link to the actual EDGAR filing index pages.
    assert "https://www.sec.gov/Archives/edgar/data/1/000000000125000004/0000000001-25-000004-index.htm" in md
    assert "derived: annual minus ytd9" in md


def test_failed_verification_blocks_approval_without_override(queue):
    draft_id = submit(queue, bad_draft())
    assert queue.get(draft_id).status == BLOCKED
    with pytest.raises(ReviewError):
        queue.approve(draft_id, reviewer="A. Analyst")
    with pytest.raises(ReviewError):
        queue.approve(draft_id, reviewer="A. Analyst", override=True)  # override still needs a reason

    queue.approve(draft_id, reviewer="A. Analyst", note="Confirmed against the press release.", override=True)
    item = queue.get(draft_id)
    assert item.status == APPROVED and item.override
    assert "Approved with verifier override" in queue.export_markdown(draft_id)


def test_only_approved_drafts_can_be_exported(queue):
    draft_id = submit(queue)
    with pytest.raises(ReviewError):
        queue.export_markdown(draft_id)


def test_rejection_requires_a_note_and_is_final(queue):
    draft_id = submit(queue)
    with pytest.raises(ReviewError):
        queue.reject(draft_id, reviewer="A. Analyst", note="")
    queue.reject(draft_id, reviewer="A. Analyst", note="Peer set is stale.")
    assert queue.get(draft_id).status == REJECTED
    with pytest.raises(ReviewError):
        queue.approve(draft_id, reviewer="A. Analyst")


def test_list_filters_by_status(queue):
    ok, blocked = submit(queue), submit(queue, bad_draft())
    assert [r["id"] for r in queue.list(PENDING)] == [ok]
    assert [r["id"] for r in queue.list(BLOCKED)] == [blocked]
    assert len(queue.list()) == 2
