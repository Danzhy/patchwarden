"""Pydantic models shared by every stage: findings, pre-classification, scan results, edits.

VerifyResult and RunState are added in M3-M4, where they're first used.
"""

from enum import StrEnum

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
