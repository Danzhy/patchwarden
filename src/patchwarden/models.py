"""Pydantic models shared by every stage: findings, pre-classification, scan results, edits,
verification."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Region(BaseModel):
    """1-based lines, as in SARIF. Columns are optional; CodeQL sometimes omits them."""

    start_line: int
    end_line: int
    start_col: int | None = None
    end_col: int | None = None


class Finding(BaseModel):
    tool: str  # "ruff", "bandit", "codeql"
    rule_id: str  # namespaced, e.g. "ruff:F401", "bandit:B602", "codeql:py/unused-import"
    message: str
    file: str  # repo-relative POSIX path
    region: Region
    snippet: str  # source text of the region's lines
    fingerprint: str  # stable across line shifts; see analyzers/sarif.py
    severity: str | None = None
    confidence: str | None = None
    tool_fixable: bool = False  # the analyzer offered its own fix (ruff `fixes`)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.region.start_line}"


class Decision(StrEnum):
    """What happens to a finding. Ordered by caution: auto_fix < suggest < escalate."""

    auto_fix = "auto_fix"
    suggest = "suggest"
    escalate = "escalate"
    false_positive = "false_positive"


class PreClassKind(StrEnum):
    """The policy verdict before any LLM sees the finding (policy.pre_classify)."""

    always_escalate = "always_escalate"
    protected_path = "protected_path"
    auto_fix_allowed = "auto_fix_allowed"
    llm_decides = "llm_decides"


class PreClass(BaseModel):
    kind: PreClassKind
    reason: str
    matched_pattern: str | None = None


class ScanResult(BaseModel):
    repo: str
    scope_mode: str  # "diff" or "full"
    files_scanned: list[str]
    analyzers_run: list[str]
    analyzers_skipped: dict[str, str] = Field(default_factory=dict)  # name -> reason
    findings: list[Finding]
    preclass: dict[str, PreClass]  # fingerprint -> verdict
    config_hash: str


class ClampResult(BaseModel):
    """The decision after policy.clamp. `clamped` means policy overrode the LLM."""

    decision: Decision
    clamped: bool
    reason: str


class EditBlock(BaseModel):
    """One SEARCH/REPLACE edit. `search` must occur exactly once in `file`."""

    file: str
    search: str
    replace: str


class ViolationKind(StrEnum):
    """Ways a diff breaks policy (policy.check_diff). Any one of them blocks the fix."""

    syntax_error = "syntax_error"
    suppression_added = "suppression_added"
    test_file_touched = "test_file_touched"
    protected_file_touched = "protected_file_touched"
    other_file_touched = "other_file_touched"
    too_many_lines = "too_many_lines"
    definition_removed = "definition_removed"
    signature_changed = "signature_changed"


class Violation(BaseModel):
    kind: ViolationKind
    file: str
    detail: str


class TriageOutput(BaseModel):
    """The Triage agent's JSON reply. Policy (clamp) has the last word on `decision`."""

    decision: Decision
    confidence: float = Field(ge=0, le=1)
    reason: str
    risk_notes: str = ""


class TestStatus(StrEnum):
    __test__ = False  # not a pytest test class

    passed = "passed"
    failed = "failed"
    timeout = "timeout"
    error = "error"  # the command couldn't run
    skipped = "skipped"  # no test_command, or the tests already failed before any change


class VerifyResult(BaseModel):
    """The code checks on one fix (verify.verify_fix). Findings are "rule at file:line: message"
    strings: this is what the Fixer gets back as feedback and what the trace stores."""

    syntax_ok: bool
    target_gone: bool = False
    new_findings: list[str] = Field(default_factory=list)
    # Other findings the fix made disappear: a fix changes one finding's code, nothing else.
    also_resolved: list[str] = Field(default_factory=list)
    tests: TestStatus = TestStatus.skipped
    tests_detail: str = ""  # why skipped, or the tail of the output on failure
    error: str | None = None  # the re-scan itself failed

    @property
    def passed(self) -> bool:
        return not self.failures()

    def failures(self) -> list[str]:
        if not self.syntax_ok:
            return [f"the edited file doesn't parse ({self.error})"]
        if self.error:
            return [f"re-scan failed: {self.error}"]
        out = []
        if not self.target_gone:
            out.append("the analyzer still reports the finding")
        out += [f"new finding: {f}" for f in self.new_findings]
        out += [f"also changes code flagged by another finding: {f}" for f in self.also_resolved]
        if self.tests in (TestStatus.failed, TestStatus.timeout, TestStatus.error):
            out.append(f"tests {self.tests}: {self.tests_detail}")
        return out


class VerifierOutput(BaseModel):
    """The Verifier agent's JSON reply: an independent review of the diff."""

    verdict: Literal["pass", "fail"]
    reason: str
    behaviour_change_risk: Literal["low", "med", "high"]


class FindingStatus(StrEnum):
    fixed = "fixed"  # in the patch
    suggested = "suggested"  # a proposed diff in the report, not in the patch
    escalated = "escalated"  # a human decides; the report says why
    false_positive = "false_positive"
    failed = "failed"  # should have been fixed, but no edit could be applied
    not_triaged = "not_triaged"  # --no-llm


class FindingOutcome(BaseModel):
    finding: Finding
    preclass: PreClass
    triage: TriageOutput | None = None
    clamp: ClampResult | None = None
    status: FindingStatus
    fixed_by: str | None = None  # "ruff" or "fixer"
    fix_rounds: int = 0
    rationale: str = ""
    diff: str = ""
    violations: list[Violation] = Field(default_factory=list)
    verify: VerifyResult | None = None  # the last fix's code checks
    verifier: VerifierOutput | None = None  # the last fix's review
    error: str | None = None
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        return self.status in (FindingStatus.escalated, FindingStatus.failed)
