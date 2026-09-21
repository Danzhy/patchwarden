"""Verifier: an independent review of one fix. It sees the finding, the diff and the results of
the code checks, not the Fixer's rationale: the writer doesn't grade its own work."""

from patchwarden.agents.prompts import load, untrusted
from patchwarden.agents.triage import finding_header, rule_description
from patchwarden.llm import LLMClient
from patchwarden.models import Finding, TestStatus, VerifierOutput, VerifyResult


def checks_summary(result: VerifyResult, test_command: str | None) -> str:
    lines = [
        "- The edited file parses.",
        "- Re-scan: the finding is gone, and no new or other findings changed.",
    ]
    if result.tests == TestStatus.passed:
        lines.append(f"- The repository's tests pass (`{test_command}`).")
    else:
        lines.append(
            f"- The tests were not run ({result.tests_detail}). Judge behaviour from the code."
        )
    return "\n".join(lines)


def verifier_messages(
    finding: Finding, diff: str, result: VerifyResult, test_command: str | None
) -> list[dict]:
    user = (
        f"{finding_header(finding)}\n\n"
        f"Rule documentation:\n{rule_description(finding)}\n\n"
        f"Automated checks:\n{checks_summary(result, test_command)}\n\n"
        f"The change (unified diff):\n{untrusted(diff, finding.file)}"
    )
    return [{"role": "system", "content": load("verifier")}, {"role": "user", "content": user}]


def review(
    llm: LLMClient,
    finding: Finding,
    diff: str,
    result: VerifyResult,
    test_command: str | None,
    parent: int | None,
) -> VerifierOutput:
    reply = llm.complete(
        "verifier",
        verifier_messages(finding, diff, result, test_command),
        schema=VerifierOutput,
        finding_id=finding.fingerprint,
        parent=parent,
    )
    assert isinstance(reply.parsed, VerifierOutput)
    return reply.parsed
