"""Triage: one finding in, a decision out. Sees the finding, ±20 lines and the rule doc; never
writes code. Its decision goes through policy.clamp before anything acts on it."""

from patchwarden.agents.prompts import load, numbered_window, untrusted
from patchwarden.analyzers.ruff import rule_doc
from patchwarden.llm import LLMClient
from patchwarden.models import Finding, PreClass, TriageOutput
from patchwarden.workspace import Workspace

CONTEXT_LINES = 20


def rule_description(finding: Finding) -> str:
    if finding.tool == "ruff":
        doc = rule_doc(finding.rule_id.removeprefix("ruff:"))
        if doc:
            return doc
    return finding.message


def finding_header(finding: Finding) -> str:
    r = finding.region
    return (
        f"Rule: {finding.rule_id}\n"
        f"Message: {finding.message}\n"
        f"Location: {finding.file}:{r.start_line}"
        + (f"-{r.end_line}" if r.end_line != r.start_line else "")
    )


def triage_messages(ws: Workspace, finding: Finding, preclass: PreClass) -> list[dict]:
    r = finding.region
    window, lo, hi = numbered_window(ws.read(finding.file), r.start_line, r.end_line, CONTEXT_LINES)
    user = (
        f"{finding_header(finding)}\n"
        f"Policy pre-classification: {preclass.kind} ({preclass.reason})\n\n"
        f"Rule documentation:\n{rule_description(finding)}\n\n"
        f'Code, lines {lo}-{hi} of {finding.file} (">" marks the finding):\n'
        f"{untrusted(window, finding.file)}"
    )
    return [{"role": "system", "content": load("triage")}, {"role": "user", "content": user}]


def triage(
    llm: LLMClient, ws: Workspace, finding: Finding, preclass: PreClass, parent: int | None
) -> TriageOutput:
    reply = llm.complete(
        "triage",
        triage_messages(ws, finding, preclass),
        schema=TriageOutput,
        finding_id=finding.fingerprint,
        parent=parent,
    )
    assert isinstance(reply.parsed, TriageOutput)
    return reply.parsed
