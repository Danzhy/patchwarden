"""Policy in code: pre_classify (M1); clamp and check_diff follow in M2.

These are pure functions. The LLM can make a decision more cautious, never less; the floor it
can't go below comes from pre_classify.
"""

from fnmatch import fnmatchcase

from patchwarden.config import Config
from patchwarden.models import Finding, PreClass, PreClassKind
from patchwarden.scope import is_test_path, match_path


def match_rule(rule_id: str, patterns: list[str]) -> str | None:
    """The first glob matching a namespaced rule id ("bandit:B602"), else None."""
    return next((p for p in patterns if fnmatchcase(rule_id, p)), None)


def pre_classify(finding: Finding, cfg: Config) -> PreClass:
    """First match wins: always_escalate -> protected_paths -> auto_fix allowlist -> LLM."""
    if pat := match_rule(finding.rule_id, cfg.always_escalate):
        return PreClass(
            kind=PreClassKind.always_escalate,
            reason=f"rule {finding.rule_id} is always escalated",
            matched_pattern=pat,
        )
    if pat := match_path(finding.file, cfg.protected_paths):
        return PreClass(
            kind=PreClassKind.protected_path,
            reason=f"{finding.file} is a protected path",
            matched_pattern=pat,
        )
    if pat := match_rule(finding.rule_id, cfg.auto_fix):
        if is_test_path(finding.file, cfg):
            # Fixes may never touch tests without review (check_diff flags it too, in M2).
            return PreClass(
                kind=PreClassKind.llm_decides,
                reason=f"{finding.rule_id} is allowlisted, but {finding.file} is a test file",
            )
        return PreClass(
            kind=PreClassKind.auto_fix_allowed,
            reason=f"rule {finding.rule_id} is on the auto-fix allowlist",
            matched_pattern=pat,
        )
    return PreClass(kind=PreClassKind.llm_decides, reason="no policy rule applies")
