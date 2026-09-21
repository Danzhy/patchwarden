"""Fixer: one finding and its file in, SEARCH/REPLACE blocks out. The blocks are applied and
judged by code (edits.apply_edits, policy.check_diff); nothing the model writes is executed."""

import re
from dataclasses import dataclass

from patchwarden.agents.prompts import load, untrusted
from patchwarden.agents.triage import finding_header, rule_description
from patchwarden.edits import EditParseError, parse_edit_blocks
from patchwarden.llm import LLMClient
from patchwarden.models import EditBlock, Finding
from patchwarden.workspace import Workspace, split_lines

WHOLE_FILE_MAX_LINES = 400
WINDOW_LINES = 80
_RATIONALE = re.compile(r"^\s*RATIONALE:\s*(.*)$", re.MULTILINE)


@dataclass
class FixProposal:
    reply: str
    rationale: str
    blocks: list[EditBlock]


def fixer_messages(ws: Workspace, finding: Finding) -> list[dict]:
    text = ws.read(finding.file)
    lines = split_lines(text)
    r = finding.region
    if len(lines) <= WHOLE_FILE_MAX_LINES:
        what, body = f"The whole file {finding.file}", text
    else:
        lo = max(1, r.start_line - WINDOW_LINES)
        hi = min(len(lines), r.end_line + WINDOW_LINES)
        what = f"Lines {lo}-{hi} of {finding.file} ({len(lines)} lines; SEARCH text must be inside)"
        body = "".join(lines[lo - 1 : hi])
    flagged = "".join(lines[r.start_line - 1 : r.end_line]) or finding.snippet
    user = (
        f"{finding_header(finding)}\n\n"
        f"Rule documentation:\n{rule_description(finding)}\n\n"
        f"The flagged line(s):\n{untrusted(flagged, finding.file)}\n\n"
        f"{what}:\n{untrusted(body, finding.file)}\n\n"
        f"Fix this one finding in {finding.file}."
    )
    return [{"role": "system", "content": load("fixer")}, {"role": "user", "content": user}]


def parse_proposal(reply: str) -> FixProposal:
    m = _RATIONALE.search(reply)
    blocks = parse_edit_blocks(reply)
    if not blocks:
        raise EditParseError("the reply has no SEARCH/REPLACE edit blocks")
    return FixProposal(reply, m.group(1).strip() if m else "", blocks)


def propose_fix(
    llm: LLMClient,
    ws: Workspace,
    finding: Finding,
    feedback: list[tuple[str, str]],
    parent: int | None,
) -> str:
    """The Fixer's raw reply (parse it with parse_proposal). `feedback`: (previous reply, what
    went wrong with it), oldest first."""
    messages = fixer_messages(ws, finding)
    for reply, error in feedback:
        messages += [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": f"That edit could not be used: {error}\n"
                "The file is unchanged. Reply again in the required format, with SEARCH text "
                "copied exactly from the current file.",
            },
        ]
    return llm.complete("fixer", messages, finding_id=finding.fingerprint, parent=parent).text
