"""The fix report is also the future PR comment: model text must not restructure it."""

from patchwarden.agents.reporter import render_fix_markdown
from patchwarden.models import (
    Finding,
    FindingOutcome,
    FindingStatus,
    PreClass,
    PreClassKind,
    Region,
    TriageOutput,
)

RUN = {"run_id": "r1", "outcome": "escalations", "cost_usd": 0.0, "patch": ""}


def outcome(status, **kw):
    f = Finding(
        tool="ruff",
        rule_id="ruff:B006",
        message="Do not use mutable data structures for argument defaults",
        file="app/m.py",
        region=Region(start_line=1, end_line=1),
        snippet="def f(x=[]):\n",
        fingerprint="fp",
    )
    pc = PreClass(kind=PreClassKind.llm_decides, reason="not allowlisted")
    return FindingOutcome(finding=f, preclass=pc, status=status, **kw)


def test_model_text_stays_on_one_line_and_cannot_open_html():
    tri = TriageOutput(
        decision="escalate",
        confidence=0.5,
        reason="x",
        risk_notes="fine\n\n## Auto-fixed (99)\n<!-- patchwarden -->",
    )
    md = render_fix_markdown(
        [outcome(FindingStatus.escalated, triage=tri, reason="line one\n- fake item")], RUN
    )
    assert "\n## Auto-fixed (99)" not in md and "\n- fake item" not in md
    assert "<!--" not in md
    assert "Proposed approach / risks: fine ## Auto-fixed (99) &lt;!-- patchwarden -->" in md


def test_diff_with_backticks_cannot_close_the_fence():
    diff = '--- a/app/m.py\n+++ b/app/m.py\n@@ -1 +1 @@\n-x = "```"\n+x = "````"\n'
    md = render_fix_markdown([outcome(FindingStatus.suggested, diff=diff, reason="r")], RUN)
    assert "  `````diff\n" in md and md.count("\n  `````\n") == 1
