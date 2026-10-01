"""
Human review queue for commentary drafts (SQLite, stdlib only).

Nothing the agent writes is usable until a person approves it. The state
machine is deliberately small:

    submit ──► pending_review ──approve──► approved ──export──► markdown
       │              └──────reject──────► rejected
       └─(verifier errors)─► blocked ──approve with override note──► approved
                                 └────────reject─────────────────► rejected

A blocked draft can only be approved with an explicit override note, which
is stored alongside the reviewer's name — the audit trail shows who shipped a
number the verifier couldn't confirm, and why.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.agent.drafter import Draft
from src.agent.facts import FactSheet
from src.agent.verifier import VerificationReport

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "review" / "queue.db"

PENDING, BLOCKED, APPROVED, REJECTED = "pending_review", "blocked", "approved", "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    cik               TEXT NOT NULL,
    period            TEXT NOT NULL,
    drafter           TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    draft_json        TEXT NOT NULL,
    fact_sheet_json   TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    reviewer          TEXT,
    reviewed_at       TEXT,
    review_note       TEXT,
    override          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status);
"""


class ReviewError(RuntimeError):
    pass


@dataclass
class QueueItem:
    id: int
    ticker: str
    cik: str
    period: str
    drafter: str
    status: str
    created_at: str
    draft: Draft
    fact_sheet: FactSheet
    verification: dict
    reviewer: str | None
    reviewed_at: str | None
    review_note: str | None
    override: bool


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ReviewQueue:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def submit(self, sheet: FactSheet, draft: Draft, report: VerificationReport) -> int:
        status = PENDING if report.passed else BLOCKED
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO drafts (ticker, cik, period, drafter, status, created_at, draft_json, "
                "fact_sheet_json, verification_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sheet.ticker,
                    sheet.cik,
                    sheet.period,
                    draft.drafter,
                    status,
                    _now(),
                    draft.to_json(),
                    sheet.to_json(),
                    json.dumps(report.to_dict()),
                ),
            )
            return int(cur.lastrowid)

    def get(self, draft_id: int) -> QueueItem:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise ReviewError(f"No draft with id {draft_id}.")
        return QueueItem(
            id=row["id"],
            ticker=row["ticker"],
            cik=row["cik"],
            period=row["period"],
            drafter=row["drafter"],
            status=row["status"],
            created_at=row["created_at"],
            draft=Draft.from_json(row["draft_json"]),
            fact_sheet=FactSheet.from_json(row["fact_sheet_json"]),
            verification=json.loads(row["verification_json"]),
            reviewer=row["reviewer"],
            reviewed_at=row["reviewed_at"],
            review_note=row["review_note"],
            override=bool(row["override"]),
        )

    def list(self, status: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT id, ticker, period, drafter, status, created_at, reviewer FROM drafts"
        args: tuple = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        with self._connect() as conn:
            return conn.execute(query + " ORDER BY id DESC", args).fetchall()

    def approve(self, draft_id: int, reviewer: str, note: str | None = None, override: bool = False) -> None:
        item = self.get(draft_id)
        if item.status == BLOCKED:
            if not (override and note):
                raise ReviewError(
                    f"Draft {draft_id} failed verification. Approving it requires override=True and a note "
                    "explaining why the flagged numbers are acceptable."
                )
        elif item.status != PENDING:
            raise ReviewError(f"Draft {draft_id} is {item.status}; only pending or blocked drafts can be approved.")
        self._close(draft_id, APPROVED, reviewer, note, override=item.status == BLOCKED)

    def reject(self, draft_id: int, reviewer: str, note: str) -> None:
        item = self.get(draft_id)
        if item.status not in (PENDING, BLOCKED):
            raise ReviewError(f"Draft {draft_id} is already {item.status}.")
        if not note:
            raise ReviewError("A rejection needs a note so the next draft can address it.")
        self._close(draft_id, REJECTED, reviewer, note, override=False)

    def _close(self, draft_id: int, status: str, reviewer: str, note: str | None, override: bool) -> None:
        if not reviewer:
            raise ReviewError("Reviewer name is required.")
        with self._connect() as conn:
            conn.execute(
                "UPDATE drafts SET status = ?, reviewer = ?, reviewed_at = ?, review_note = ?, override = ? "
                "WHERE id = ?",
                (status, reviewer, _now(), note, int(override), draft_id),
            )

    def export_markdown(self, draft_id: int) -> str:
        item = self.get(draft_id)
        if item.status != APPROVED:
            raise ReviewError(f"Draft {draft_id} is {item.status}; only approved commentary can be exported.")
        return render_markdown(item)


SECTION_TITLES = {
    "headline": "Headline",
    "revenue": "Revenue",
    "margins": "Margins",
    "operating_expenses": "Operating expenses",
    "profitability": "Profitability",
    "peer_context": "Peer and sector context",
    "data_caveats": "Data caveats",
}


def render_markdown(item: QueueItem) -> str:
    """Commentary with a numbered source note on every sentence that uses a
    number, linking to the SEC filing(s) the figure came from."""
    sheet, draft = item.fact_sheet, item.draft
    lines = [f"# {sheet.company_name} ({sheet.ticker}) — {sheet.period} variance commentary", ""]
    lines.append(
        f"*Approved by {item.reviewer} on {item.reviewed_at}. Drafted by `{item.drafter}`; "
        f"GAAP figures from SEC XBRL filings.*"
    )
    if item.override:
        lines.append(f"\n> **Approved with verifier override:** {item.review_note}")
    lines.append("")

    footnotes: list[str] = []
    note_index: dict[str, int] = {}
    for section in SECTION_TITLES:
        sentences = [s for s in draft.sentences if s.section == section]
        if not sentences:
            continue
        lines += [f"## {SECTION_TITLES[section]}", ""]
        paragraph = []
        for s in sentences:
            refs = []
            for fid in s.fact_ids:
                if fid not in note_index:
                    note_index[fid] = len(note_index) + 1
                    footnotes.append(_footnote(note_index[fid], fid, sheet))
                refs.append(f"[^{note_index[fid]}]")
            paragraph.append(s.text + "".join(refs))
        lines += [" ".join(paragraph), ""]

    if draft.open_questions:
        lines += ["## Open questions for the business", ""] + [f"- {q}" for q in draft.open_questions] + [""]
    if draft.declined:
        lines += ["## Out of scope", ""] + [f"- {d}" for d in draft.declined] + [""]
    if footnotes:
        lines += ["---", ""] + footnotes
    return "\n".join(lines) + "\n"


def _footnote(n: int, fid: str, sheet: FactSheet) -> str:
    fact = sheet.get(fid)
    text = f"[^{n}]: `{fid}` = {fact.display()}"
    if fact.formula:
        return text + f", computed as {fact.formula}({', '.join(fact.inputs)})."
    if fact.source:
        src = fact.source
        links = ", ".join(f"[{a}]({u})" for a, u in zip(src.adsh.split("|"), src.filing_urls(fact.cik), strict=True))
        tags = ", ".join(f"`{t}`" for t in dict.fromkeys(src.tag.split("|")))
        how = "" if not src.is_derived else f" (derived: {src.derivation.replace('_', ' ')})"
        return text + f"; XBRL {tags}, period ending {src.ddate}{how}; filings {links}."
    return text + (f"; {fact.note}." if fact.note else ".")
